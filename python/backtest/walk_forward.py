# python/backtest/walk_forward.py
# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward optimisation with Optuna.
#
# Splits history into rolling windows (train, test):
#   |---- train ----|-- test --|
#                   ↓ slide window forward by `test` length
#   |---- train ----|-- test --|
#
# For each window, Optuna searches strategy hyperparameters on TRAIN to
# maximise an objective (default: profit_factor, penalised by max_dd).
# Best params are then evaluated on the unseen TEST window.
# Out-of-sample TEST results are aggregated for honest performance.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

import pandas as pd

from config.settings import LOG_LEVEL
from python.backtest.engine import BacktestEngine, BacktestResult
from python.backtest.metrics import Metrics
from python.strategies.base import Strategy

logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger(__name__)

# Optuna is optional — only required at runtime, not import time
try:
    import optuna
    _HAS_OPTUNA = True
except ImportError:
    _HAS_OPTUNA = False


@dataclass
class FoldResult:
    train_start:   pd.Timestamp
    train_end:     pd.Timestamp
    test_start:    pd.Timestamp
    test_end:      pd.Timestamp
    best_params:   dict
    train_metrics: Metrics
    test_metrics:  Metrics


def _slice_frames(
    frames: dict[str, pd.DataFrame],
    start:  pd.Timestamp,
    end:    pd.Timestamp,
) -> dict[str, pd.DataFrame]:
    return {tf: df.loc[start:end] for tf, df in frames.items()}


def _objective_score(m: Metrics, dd_penalty: float = 0.5) -> float:
    """Profit factor minus a drawdown penalty. Inf → cap at 10."""
    pf = min(m.profit_factor, 10.0) if m.profit_factor != float("inf") else 10.0
    return pf - dd_penalty * (m.max_dd_pct / 10.0)


def walk_forward(
    *,
    primary_tf:    str,
    frames:        dict[str, pd.DataFrame],
    strategy_factory: Callable[[dict], Strategy],
    param_space:   Callable[["optuna.Trial"], dict],
    train_months:  int = 6,
    test_months:   int = 1,
    n_trials:      int = 30,
    starting_balance: float = 10_000.0,
) -> list[FoldResult]:
    """
    strategy_factory: callable(params) -> Strategy instance
    param_space:      callable(trial)  -> param dict (calls trial.suggest_*)
    """
    if not _HAS_OPTUNA:
        raise RuntimeError("Optuna is not installed. `pip install optuna`")

    primary = frames[primary_tf]
    if primary.empty:
        raise ValueError("No primary-TF data")

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    folds: list[FoldResult] = []
    start = primary.index.min().normalize()
    end   = primary.index.max().normalize()

    cursor = start
    while True:
        train_start = cursor
        train_end   = train_start + pd.DateOffset(months=train_months)
        test_start  = train_end
        test_end    = test_start + pd.DateOffset(months=test_months)
        if test_end > end:
            break

        train_frames = _slice_frames(frames, train_start, train_end)
        test_frames  = _slice_frames(frames, test_start,  test_end)

        # ── Optuna search on training window ────────────────────────────────
        def _trial_objective(trial: "optuna.Trial") -> float:
            params   = param_space(trial)
            strat    = strategy_factory(params)
            engine   = BacktestEngine(starting_balance=starting_balance)
            result   = engine.run(strat, train_frames)
            return _objective_score(result.metrics)

        study = optuna.create_study(direction="maximize")
        study.optimize(_trial_objective, n_trials=n_trials, show_progress_bar=False)
        best_params = study.best_params

        # ── Evaluate best params on training and on test (OOS) ──────────────
        train_strat  = strategy_factory(best_params)
        train_result = BacktestEngine(starting_balance=starting_balance).run(
            train_strat, train_frames
        )
        test_strat  = strategy_factory(best_params)
        test_result = BacktestEngine(starting_balance=starting_balance).run(
            test_strat, test_frames
        )

        fold = FoldResult(
            train_start  = train_start,
            train_end    = train_end,
            test_start   = test_start,
            test_end     = test_end,
            best_params  = best_params,
            train_metrics= train_result.metrics,
            test_metrics = test_result.metrics,
        )
        folds.append(fold)
        log.info(
            "Fold %s→%s | train PF=%.2f / test PF=%.2f | params=%s",
            train_start.date(), test_end.date(),
            fold.train_metrics.profit_factor, fold.test_metrics.profit_factor,
            best_params,
        )

        cursor = cursor + pd.DateOffset(months=test_months)

    return folds
