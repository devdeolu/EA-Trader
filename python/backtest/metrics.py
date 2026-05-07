# python/backtest/metrics.py
# ─────────────────────────────────────────────────────────────────────────────
# Performance metrics derived from a list of closed trades.
# Pure functions — easy to unit-test.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class Metrics:
    n_trades:        int
    win_rate:        float
    profit_factor:   float
    expectancy_r:    float    # average R per trade
    avg_win_r:       float
    avg_loss_r:      float
    max_dd_pct:      float
    sharpe:          float
    total_return_pct:float
    final_balance:   float


def compute_metrics(
    trades:           Sequence["Trade"],
    starting_balance: float,
    equity_curve:     Sequence[float] | None = None,
) -> Metrics:
    if not trades:
        return Metrics(0, 0, 0, 0, 0, 0, 0, 0, 0, starting_balance)

    pnls    = np.array([t.pnl for t in trades], dtype=float)
    r_mults = np.array([t.r_multiple for t in trades], dtype=float)

    wins   = pnls[pnls > 0]
    losses = pnls[pnls < 0]

    win_rate      = len(wins) / len(pnls)
    gross_win     = wins.sum()
    gross_loss    = -losses.sum()
    profit_factor = gross_win / gross_loss if gross_loss > 0 else math.inf
    avg_win_r     = float(r_mults[pnls > 0].mean()) if len(wins) else 0.0
    avg_loss_r    = float(r_mults[pnls < 0].mean()) if len(losses) else 0.0
    expectancy_r  = float(r_mults.mean())

    # Equity curve & drawdown
    if equity_curve is None:
        equity_curve = np.cumsum(pnls) + starting_balance
    eq = np.asarray(equity_curve, dtype=float)
    running_max = np.maximum.accumulate(eq)
    dd          = (running_max - eq) / running_max
    max_dd_pct  = float(dd.max() * 100) if len(dd) else 0.0

    # Sharpe on per-trade returns (annualised assuming ~252 trading days,
    # rough proxy — proper Sharpe needs time-weighted returns)
    rets = pnls / starting_balance
    sharpe = (
        float(rets.mean() / rets.std() * math.sqrt(252))
        if rets.std() > 0 else 0.0
    )

    final_balance     = float(starting_balance + pnls.sum())
    total_return_pct  = (final_balance - starting_balance) / starting_balance * 100

    return Metrics(
        n_trades        = len(trades),
        win_rate        = win_rate,
        profit_factor   = profit_factor,
        expectancy_r    = expectancy_r,
        avg_win_r       = avg_win_r,
        avg_loss_r      = avg_loss_r,
        max_dd_pct      = max_dd_pct,
        sharpe          = sharpe,
        total_return_pct= total_return_pct,
        final_balance   = final_balance,
    )
