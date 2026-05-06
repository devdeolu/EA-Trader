# tests/test_risk.py
# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for the risk gate.
# ─────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone

from python.core.data_engine import MarketSnapshot
from python.core.regime import Regime, RegimeReading
from python.core.risk import RiskGate


def _snap(spread_pips: float = 0.5, dd_pct: float = 0.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="EURUSD",
        taken_at=datetime.now(timezone.utc),
        primary_tf="M15",
        frames={},
        tick={"spread_pips": spread_pips},
        account={"drawdown_pct": dd_pct, "balance": 10000, "equity": 10000},
    )


def _reading(regime=Regime.TRENDING) -> RegimeReading:
    return RegimeReading(regime=regime, adx=28.0, bb_width=0.001, atr_ratio=1.0,
                         reasons=())


# ── A datetime inside London + NY overlap (12:00–16:00 UTC) ────────────────
LONDON_NY = datetime(2026, 1, 14, 13, 0, 0, tzinfo=timezone.utc)


def test_blocks_when_outside_session():
    gate = RiskGate(news=None)
    asia = datetime(2026, 1, 14, 2, 0, 0, tzinfo=timezone.utc)
    res = gate.check(
        snapshot=_snap(), regime=_reading(), signal_action="BUY",
        sl_price=1.0950, tp_price=1.1050, entry_price=1.1000,
        trades_today=0, consecutive_losses=0, now_utc=asia,
    )
    assert not res.allowed
    assert "outside_sessions" in res.reason


def test_blocks_when_spread_too_wide():
    gate = RiskGate(news=None)
    res = gate.check(
        snapshot=_snap(spread_pips=3.0), regime=_reading(),
        signal_action="BUY", sl_price=1.0950, tp_price=1.1050,
        entry_price=1.1000,
        trades_today=0, consecutive_losses=0, now_utc=LONDON_NY,
    )
    assert not res.allowed
    assert "spread" in res.reason


def test_blocks_when_volatile_regime():
    gate = RiskGate(news=None)
    res = gate.check(
        snapshot=_snap(), regime=_reading(Regime.VOLATILE),
        signal_action="BUY", sl_price=1.0950, tp_price=1.1050,
        entry_price=1.1000,
        trades_today=0, consecutive_losses=0, now_utc=LONDON_NY,
    )
    assert not res.allowed
    assert "regime" in res.reason


def test_blocks_when_rr_too_low():
    gate = RiskGate(news=None)
    res = gate.check(
        snapshot=_snap(), regime=_reading(),
        signal_action="BUY",
        sl_price=1.0980, tp_price=1.1010,   # RR=1.0
        entry_price=1.1000,
        trades_today=0, consecutive_losses=0, now_utc=LONDON_NY,
    )
    assert not res.allowed
    assert "rr_" in res.reason


def test_allows_clean_signal():
    gate = RiskGate(news=None)
    res = gate.check(
        snapshot=_snap(), regime=_reading(),
        signal_action="BUY",
        sl_price=1.0950, tp_price=1.1100,   # RR=2.0
        entry_price=1.1000,
        trades_today=0, consecutive_losses=0, now_utc=LONDON_NY,
    )
    assert res.allowed
