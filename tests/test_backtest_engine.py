# tests/test_backtest_engine.py
"""
End-to-end backtest test using synthetic data.

We build a deterministic uptrending market so the trend-pullback strategy
can plausibly fire, and verify the engine:
  - Runs to completion without errors
  - Produces a valid metrics object
  - Never produces a final balance below 0
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from python.backtest.engine import BacktestEngine
from python.backtest.metrics import compute_metrics
from python.strategies.trend_pullback import TrendPullback


def _make_frame(
    n:        int,
    freq:     str,
    base:     float = 1.10,
    drift:    float = 0.00002,
    noise:    float = 0.00010,
    seed:     int   = 0,
) -> pd.DataFrame:
    rng    = np.random.default_rng(seed)
    times  = pd.date_range("2024-01-01", periods=n, freq=freq, tz=timezone.utc)
    closes = base + np.cumsum(rng.normal(drift, noise, n))
    opens  = np.r_[closes[0], closes[:-1]]
    highs  = np.maximum(opens, closes) + rng.uniform(0, noise, n)
    lows   = np.minimum(opens, closes) - rng.uniform(0, noise, n)
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes,
         "volume": rng.integers(100, 1000, n)},
        index=times,
    ).rename_axis("time")


def _build_frames() -> dict[str, pd.DataFrame]:
    # M15 = 2000 bars (~21 days). Larger TFs derived by simple resampling.
    m15 = _make_frame(2000, "15min", seed=1)
    rule_map = {"M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}

    def _resample(rule: str) -> pd.DataFrame:
        agg = m15.resample(rule).agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna()
        return agg

    return {
        "M5":  _resample(rule_map["M5"]),
        "M15": m15,
        "H1":  _resample(rule_map["H1"]),
        "H4":  _resample(rule_map["H4"]),
        "D1":  _resample(rule_map["D1"]),
    }


def test_backtest_engine_runs_end_to_end():
    frames   = _build_frames()
    strategy = TrendPullback()
    engine   = BacktestEngine(starting_balance=10_000.0, warmup_bars=300)
    result   = engine.run(strategy, frames)

    assert result.starting_balance == 10_000.0
    assert isinstance(result.trades, list)
    assert result.metrics.final_balance >= 0
    # Equity curve always starts at the starting balance
    assert result.equity_curve[0] == 10_000.0
    # n_trades consistent with trade list
    assert result.metrics.n_trades == len(result.trades)


def test_metrics_on_empty_trades():
    m = compute_metrics([], starting_balance=10_000.0)
    assert m.n_trades       == 0
    assert m.final_balance  == 10_000.0
    assert m.profit_factor  == 0
