# python/core/sizing.py
# ─────────────────────────────────────────────────────────────────────────────
# Position sizing.
#
# Converts (risk_pct, SL distance) → lots, given the current account balance
# and the symbol's contract metadata. Snaps to the broker's lot step.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import math

from config.settings import LOG_LEVEL

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("sizing")


def compute_lots(
    *,
    balance:       float,
    risk_pct:      float,
    entry_price:   float,
    sl_price:      float,
    contract_size: float,
    pip_size:      float,
    min_lot:       float,
    max_lot:       float,
    lot_step:      float,
    tick_size:     float | None = None,
    tick_value:    float | None = None,
) -> float:
    """
    Return the lot size that risks `risk_pct` of `balance` if SL is hit.

    Two modes:
      - If `tick_size` and `tick_value` are provided (preferred), uses MT5's
        broker-supplied per-tick value. Works correctly for ANY symbol
        (FX majors, JPY crosses, gold, indices) with no currency math.
      - Otherwise falls back to the simple `sl_distance * contract_size`
        formula — only correct when quote currency == account currency.
    """
    if balance <= 0 or risk_pct <= 0:
        return 0.0

    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        log.warning("Invalid SL distance — returning 0 lots")
        return 0.0

    risk_dollars = balance * (risk_pct / 100.0)

    if tick_size and tick_value and tick_size > 0:
        # Loss per 1.00 lot if SL hit, in account currency
        loss_per_unit = (sl_distance / tick_size) * tick_value
    else:
        loss_per_unit = sl_distance * contract_size

    if loss_per_unit <= 0:
        return 0.0

    raw_lots = risk_dollars / loss_per_unit

    # Snap down to lot_step (with FP guard so 0.2/0.01 doesn't underflow to 0.19)
    if lot_step > 0:
        raw_lots = math.floor(raw_lots / lot_step + 1e-9) * lot_step

    lots = max(min_lot, min(raw_lots, max_lot))
    return round(lots, 2)
