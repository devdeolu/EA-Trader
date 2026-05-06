# python/core/regime.py
# ─────────────────────────────────────────────────────────────────────────────
# Rules-based market regime classifier.
#
# Phase 2 starts simple and explainable: ADX + Bollinger band width + ATR
# expansion. Phase 3+ can swap this out for a Hidden Markov Model with the
# same `classify(snapshot) -> Regime` interface — strategies don't care.
#
# Regimes:
#   TRENDING  — directional follow-through, pullback strategies preferred
#   RANGING   — mean reversion preferred
#   VOLATILE  — chaotic / news-driven; no new entries
#   QUIET     — too dead to trade (Asian session on EURUSD, etc.)
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from config.settings import LOG_LEVEL, REGIME_ADX_RANGE, REGIME_ADX_TREND
from python.core.data_engine import MarketSnapshot

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("regime")


class Regime(str, Enum):
    TRENDING = "trending"
    RANGING  = "ranging"
    VOLATILE = "volatile"
    QUIET    = "quiet"


@dataclass(frozen=True)
class RegimeReading:
    regime:    Regime
    adx:       float
    bb_width:  float
    atr_ratio: float
    reasons:   tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "regime":    self.regime.value,
            "adx":       round(self.adx, 2),
            "bb_width":  round(self.bb_width, 5),
            "atr_ratio": round(self.atr_ratio, 2),
            "reasons":   list(self.reasons),
        }


def _atr_ratio(df: pd.DataFrame, lookback: int = 20) -> float:
    """Current ATR relative to its own `lookback`-bar mean. >1.5 = expansion."""
    if "atr" not in df.columns or len(df) < lookback + 2:
        return 1.0
    recent  = df["atr"].iloc[-2]
    average = df["atr"].iloc[-(lookback + 2):-2].mean()
    if average <= 0:
        return 1.0
    return float(recent / average)


def classify(snapshot: MarketSnapshot, tf: str | None = None) -> RegimeReading:
    """
    Classify the current regime on `tf` (defaults to the snapshot's primary).
    """
    df = snapshot.frames[tf or snapshot.primary_tf]
    if df.empty:
        return RegimeReading(Regime.QUIET, 0, 0, 1.0, ("no_data",))

    last      = df.iloc[-2]
    adx       = float(last.get("adx", 0))
    bb_width  = float(last.get("bb_width", 0))
    atr_ratio = _atr_ratio(df)

    reasons: list[str] = []

    # ── VOLATILE: ATR runaway → no trade ─────────────────────────────────
    if atr_ratio >= 2.0:
        reasons.append(f"atr_ratio={atr_ratio:.2f}>=2.0")
        return RegimeReading(Regime.VOLATILE, adx, bb_width, atr_ratio, tuple(reasons))

    # ── QUIET: dead market ────────────────────────────────────────────────
    if atr_ratio <= 0.5 and adx < REGIME_ADX_RANGE:
        reasons.append(f"atr_ratio={atr_ratio:.2f}<=0.5")
        reasons.append(f"adx={adx:.1f}<{REGIME_ADX_RANGE}")
        return RegimeReading(Regime.QUIET, adx, bb_width, atr_ratio, tuple(reasons))

    # ── TRENDING ──────────────────────────────────────────────────────────
    if adx >= REGIME_ADX_TREND:
        reasons.append(f"adx={adx:.1f}>={REGIME_ADX_TREND}")
        return RegimeReading(Regime.TRENDING, adx, bb_width, atr_ratio, tuple(reasons))

    # ── RANGING (default when ADX is below trend threshold) ──────────────
    reasons.append(f"adx={adx:.1f}<{REGIME_ADX_TREND}")
    return RegimeReading(Regime.RANGING, adx, bb_width, atr_ratio, tuple(reasons))
