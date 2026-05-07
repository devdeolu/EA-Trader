# tests/test_trend_pullback.py
# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for the trend-pullback strategy. Constructs synthetic bars so
# the test runs without MT5.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from python.core.data_engine import MarketSnapshot
from python.core.indicators import add_indicators
from python.core.regime import Regime, RegimeReading
from python.strategies.trend_pullback import TrendPullback


def _frame_from_closes(closes: np.ndarray, freq: str) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    df = pd.DataFrame({
        "open":   closes,
        "high":   closes + 0.0003,
        "low":    closes - 0.0003,
        "close":  closes,
        "volume": 100,
    }, index=idx)
    return add_indicators(df)


def _trending_snapshot() -> MarketSnapshot:
    closes_d1  = 1.10 + np.linspace(0, 0.05, 250)
    closes_h1  = 1.10 + np.linspace(0, 0.04, 300)
    closes_m15 = 1.10 + np.linspace(0, 0.03, 300)

    return MarketSnapshot(
        symbol="EURUSD",
        taken_at=datetime.now(timezone.utc),
        primary_tf="M15",
        frames={
            "M15": _frame_from_closes(closes_m15, "15min"),
            "H1":  _frame_from_closes(closes_h1,  "1h"),
            "D1":  _frame_from_closes(closes_d1,  "1D"),
        },
        tick={"bid": 1.13, "ask": 1.13001, "spread_pips": 0.1},
        account={"balance": 10000, "equity": 10000, "drawdown_pct": 0},
        symbol_info={"contract_size": 100000, "pip_size": 0.0001,
                     "min_lot": 0.01, "max_lot": 100.0, "lot_step": 0.01},
    )


def _trending_regime() -> RegimeReading:
    return RegimeReading(regime=Regime.TRENDING, adx=30.0,
                         bb_width=0.001, atr_ratio=1.0, reasons=())


def test_returns_none_when_regime_not_trending():
    strat = TrendPullback()
    snap  = _trending_snapshot()
    ranging = RegimeReading(regime=Regime.RANGING, adx=15.0,
                            bb_width=0.001, atr_ratio=1.0, reasons=())
    assert strat.on_tick(snap, ranging) is None


def test_returns_none_when_no_pullback_signal():
    """Synthetic linear uptrend has no pullback, so should return None."""
    strat = TrendPullback()
    snap  = _trending_snapshot()
    # Linear ramp leaves no lower wick → strategy must reject
    assert strat.on_tick(snap, _trending_regime()) is None


def test_handles_empty_frames():
    strat = TrendPullback()
    snap = MarketSnapshot(
        symbol="EURUSD",
        taken_at=datetime.now(timezone.utc),
        primary_tf="M15",
        frames={"M15": pd.DataFrame(), "H1": pd.DataFrame(), "D1": pd.DataFrame()},
    )
    assert strat.on_tick(snap, _trending_regime()) is None
