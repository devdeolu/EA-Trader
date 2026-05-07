# tests/test_sizing.py
# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for position sizing.
# ─────────────────────────────────────────────────────────────────────────────

from python.core.sizing import compute_lots


EURUSD = dict(
    contract_size=100000,
    pip_size=0.0001,
    min_lot=0.01,
    max_lot=100.0,
    lot_step=0.01,
)


def test_zero_balance_returns_zero():
    lots = compute_lots(balance=0, risk_pct=1.0,
                        entry_price=1.10, sl_price=1.095, **EURUSD)
    assert lots == 0.0


def test_invalid_sl_returns_zero():
    lots = compute_lots(balance=10000, risk_pct=1.0,
                        entry_price=1.10, sl_price=1.10, **EURUSD)
    assert lots == 0.0


def test_basic_eurusd_sizing():
    # $10,000 × 1% = $100 risk; 50 pips SL = 0.005 price diff;
    # loss/lot = 0.005 × 100,000 = $500; lots = 100/500 = 0.20
    lots = compute_lots(balance=10000, risk_pct=1.0,
                        entry_price=1.1000, sl_price=1.0950, **EURUSD)
    assert abs(lots - 0.20) < 1e-6


def test_snaps_to_lot_step():
    # Tight SL → fractional lots that must snap down to 0.01 step
    lots = compute_lots(balance=1000, risk_pct=1.0,
                        entry_price=1.1000, sl_price=1.0993, **EURUSD)
    # 0.0007 × 100000 = $70/lot, $10 risk → ~0.142 → snaps to 0.14
    assert lots == 0.14


def test_respects_min_lot():
    lots = compute_lots(balance=100, risk_pct=0.5,
                        entry_price=1.1000, sl_price=1.0900, **EURUSD)
    assert lots >= 0.01
