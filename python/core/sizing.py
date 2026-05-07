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
) -> float:
    """
    Return the lot size that risks `risk_pct` of `balance` if SL is hit.

    Simplified pip-value model that's accurate for quote-currency = account
    currency (e.g. EURUSD on a USD account). For cross pairs you'd convert
    with a pip-value lookup; we keep this explicit and small for now.
    """
    if balance <= 0 or risk_pct <= 0:
        return 0.0

    sl_distance = abs(entry_price - sl_price)
    if sl_distance <= 0:
        log.warning("Invalid SL distance — returning 0 lots")
        return 0.0

    risk_dollars     = balance * (risk_pct / 100.0)
    loss_per_unit    = sl_distance * contract_size  # per 1.00 lot
    if loss_per_unit <= 0:
        return 0.0

    raw_lots = risk_dollars / loss_per_unit

    # Snap down to lot_step (with FP guard so 0.2/0.01 doesn't underflow to 0.19)
    if lot_step > 0:
        raw_lots = math.floor(raw_lots / lot_step + 1e-9) * lot_step

    lots = max(min_lot, min(raw_lots, max_lot))
    return round(lots, 2)
