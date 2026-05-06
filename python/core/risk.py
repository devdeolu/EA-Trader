# python/core/risk.py
# ─────────────────────────────────────────────────────────────────────────────
# Pre-signal risk gate.
#
# Every candidate signal MUST pass this gate before being published. The gate
# is intentionally explicit — every block reason is named and logged so the
# evolution engine can later analyse which gates fire most often.
#
# The MQL5 receiver enforces these rules independently. This module is the
# Python-side fast-fail so the bridge is not spammed with rejected signals.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from config.settings import (
    LOG_LEVEL,
    MAX_ACCOUNT_DRAWDOWN_PCT,
    MAX_CONSECUTIVE_LOSSES,
    MAX_DAILY_DRAWDOWN_PCT,
    MAX_SPREAD_PIPS,
    MAX_TRADES_PER_DAY,
    MIN_RR_RATIO,
    SESSIONS,
    TRADE_SESSIONS,
)
from python.core.data_engine import MarketSnapshot
from python.core.regime import Regime, RegimeReading
from python.utils.news_guard import NewsGuard

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("risk")


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason:  str = ""

    @classmethod
    def ok(cls) -> "GateResult":
        return cls(True, "")

    @classmethod
    def block(cls, reason: str) -> "GateResult":
        return cls(False, reason)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _in_session(now_utc: datetime, sessions: list[str]) -> bool:
    t = now_utc.time()
    for s in sessions:
        cfg = SESSIONS.get(s)
        if not cfg:
            continue
        start = time.fromisoformat(cfg["start"])
        end   = time.fromisoformat(cfg["end"])
        if start <= t <= end:
            return True
    return False


# ── Gate ────────────────────────────────────────────────────────────────────

class RiskGate:
    """
    Stateless pre-signal gate. Owns no mutable state — just composes inputs.
    Daily counters live in the trade logger / account state, not here.

    Order of checks is deliberate (cheapest → most expensive):
        1. Session window
        2. News blackout
        3. Spread
        4. Account drawdown
        5. Daily trade count / consecutive losses
        6. Regime allowed
        7. R:R ratio
    """

    def __init__(self, news: Optional[NewsGuard] = None):
        self.news = news

    def check(
        self,
        *,
        snapshot:        MarketSnapshot,
        regime:          RegimeReading,
        signal_action:   str,             # "BUY" | "SELL"
        sl_price:        float,
        tp_price:        float,
        entry_price:     float,
        trades_today:    int,
        consecutive_losses: int,
        now_utc:         Optional[datetime] = None,
    ) -> GateResult:
        now = now_utc or datetime.now(timezone.utc)

        # ── 1. Session ───────────────────────────────────────────────────
        if not _in_session(now, TRADE_SESSIONS):
            return GateResult.block(f"outside_sessions:{TRADE_SESSIONS}")

        # ── 2. News ──────────────────────────────────────────────────────
        if self.news is not None and self.news.is_blackout(now):
            return GateResult.block("news_blackout")

        # ── 3. Spread ────────────────────────────────────────────────────
        spread = snapshot.tick.get("spread_pips", 0)
        if spread > MAX_SPREAD_PIPS:
            return GateResult.block(f"spread_{spread}>max_{MAX_SPREAD_PIPS}")

        # ── 4. Drawdown ──────────────────────────────────────────────────
        dd = snapshot.account.get("drawdown_pct", 0)
        if dd >= MAX_ACCOUNT_DRAWDOWN_PCT:
            return GateResult.block(f"account_dd_{dd}>={MAX_ACCOUNT_DRAWDOWN_PCT}")
        if dd >= MAX_DAILY_DRAWDOWN_PCT:
            return GateResult.block(f"daily_dd_{dd}>={MAX_DAILY_DRAWDOWN_PCT}")

        # ── 5. Daily counters ────────────────────────────────────────────
        if trades_today >= MAX_TRADES_PER_DAY:
            return GateResult.block(f"max_trades_day_{MAX_TRADES_PER_DAY}")
        if consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            return GateResult.block(f"consecutive_losses_{MAX_CONSECUTIVE_LOSSES}")

        # ── 6. Regime ────────────────────────────────────────────────────
        if regime.regime in (Regime.VOLATILE, Regime.QUIET):
            return GateResult.block(f"regime_{regime.regime.value}")

        # ── 7. R:R ───────────────────────────────────────────────────────
        risk   = abs(entry_price - sl_price)
        reward = abs(tp_price - entry_price)
        if risk <= 0:
            return GateResult.block("invalid_sl")
        rr = reward / risk
        if rr < MIN_RR_RATIO:
            return GateResult.block(f"rr_{rr:.2f}<{MIN_RR_RATIO}")

        return GateResult.ok()
