# python/strategies/base.py
# ─────────────────────────────────────────────────────────────────────────────
# Strategy contract.
#
# Every strategy module subclasses Strategy and returns zero or one Candidate
# per tick. The orchestrator runs each candidate through the risk gate, then
# converts approved ones to ZMQ signals.
#
# Strategies are PURE: they read a MarketSnapshot + RegimeReading, and return
# a candidate or None. They never touch MT5, the bridge, the DB, or time.
# That keeps them trivially backtestable.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from python.core.data_engine import MarketSnapshot
from python.core.regime import RegimeReading


@dataclass(frozen=True)
class Candidate:
    """
    A proposed trade. Prices are absolute, lots are computed downstream from
    risk_pct + SL distance — strategies don't size positions.
    """
    action:        str                 # "BUY" | "SELL"
    entry_price:   float
    sl_price:      float
    tp_price:      float
    tier:          str                 # "A" | "B"
    strategy:      str
    score:         float               # 0–10 quality score
    risk_pct:      float               # % of balance to risk on this trade
    rationale:     str = ""            # human-readable why


class Strategy(ABC):
    """Every strategy implements name + on_tick."""

    name: str = "base"

    @abstractmethod
    def on_tick(
        self,
        snapshot: MarketSnapshot,
        regime:   RegimeReading,
    ) -> Optional[Candidate]:
        """Return at most one Candidate per tick, or None."""
        raise NotImplementedError
