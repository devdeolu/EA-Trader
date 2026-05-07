# python/strategies/trend_pullback.py
# ─────────────────────────────────────────────────────────────────────────────
# Trend pullback strategy — the v9 idea, ported.
#
# Entry logic (long; mirror for short):
#   1. D1 macro filter:     close > EMA_MACRO  (uptrend bias)
#   2. H1 trend confirm:    EMA_FAST > EMA_SLOW
#   3. M15 pullback:        close pulled back to EMA_FAST
#                           AND last bar shows a bullish reaction
#                              (close > open, lower wick > body * 0.5)
#   4. RSI not overbought:  rsi < 70
#   5. Regime must be trending (enforced via regime gate, also asserted here)
#
# SL: below the pullback's swing low minus 0.5 × ATR.
# TP: 2.0 × R from entry (we let the partial-TP / trailing logic in the
#     MQL5 layer manage the runner — the published TP is the secondary).
#
# Score (0–10) composes: trend strength (ADX), pullback depth, candle quality.
# Tier A when score >= 7, otherwise Tier B.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from config.settings import (
    ENABLE_FILTERS,
    FILTER_ATR_RATIO_MIN,
    FILTER_D1_SLOPE_MIN,
    FILTER_H1_ADX_MIN,
    LOG_LEVEL,
    MIN_RR_RATIO,
    PARTIAL_TP_R,
    STRICT_TIER_A,
    TIER_A_RISK_PCT,
    TIER_B_RISK_PCT,
)
from config import settings as _settings
from python.core.data_engine import MarketSnapshot
from python.core.regime import Regime, RegimeReading
from python.strategies.base import Candidate, Strategy

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("strategy.trend_pullback")


SL_ATR_BUFFER     = 0.5     # SL = swing extreme +/- this × ATR
SWING_LOOKBACK    = 5       # bars used to find recent swing high/low
RSI_OVERBOUGHT    = 70.0
RSI_OVERSOLD      = 30.0
TIER_A_SCORE      = 7.0     # promote to Tier A risk size at this score
MIN_QUALITY_SCORE = 4.5     # reject signals below this when STRICT_TIER_A=True
TARGET_R          = max(2.0, MIN_RR_RATIO)


def _params_for(symbol: str) -> dict:
    """Return per-symbol strategy params with module-level defaults as fallback."""
    overrides = getattr(_settings, "SYMBOL_PARAMS", {}).get(symbol, {})
    return {
        "target_r":       overrides.get("target_r",       TARGET_R),
        "sl_atr_buffer":  overrides.get("sl_atr_buffer",  SL_ATR_BUFFER),
        "enable_filters": overrides.get("enable_filters", ENABLE_FILTERS),
        "strict_tier_a":  overrides.get("strict_tier_a",  STRICT_TIER_A),
    }


class TrendPullback(Strategy):
    name = "trend_pullback"

    def on_tick(
        self,
        snapshot: MarketSnapshot,
        regime:   RegimeReading,
    ) -> Optional[Candidate]:
        # Hard pre-checks the gate also covers — short-circuit here to avoid
        # paying for full evaluation when the regime is wrong.
        if regime.regime != Regime.TRENDING:
            return None

        m15 = snapshot.frames.get("M15")
        h1  = snapshot.frames.get("H1")
        d1  = snapshot.frames.get("D1")
        if m15 is None or h1 is None or d1 is None:
            return None
        if m15.empty or h1.empty or d1.empty:
            return None

        last_m15 = m15.iloc[-2]   # last closed M15 bar
        last_h1  = h1.iloc[-2]
        last_d1  = d1.iloc[-2]

        params = _params_for(snapshot.symbol)

        # ── Macro bias ───────────────────────────────────────────────────
        long_bias  = last_d1["close"] > last_d1["ema_macro"]
        short_bias = last_d1["close"] < last_d1["ema_macro"]

        # ── H1 trend confirmation ────────────────────────────────────────
        h1_up   = last_h1["ema_fast"] > last_h1["ema_slow"]
        h1_down = last_h1["ema_fast"] < last_h1["ema_slow"]

        # ── Structural filters (Option A) ───────────────────────────────
        if params["enable_filters"]:
            # 1) H1 ADX strength — multi-TF trend confirmation
            h1_adx = float(last_h1.get("adx", 0))
            if h1_adx < FILTER_H1_ADX_MIN:
                return None

            # 2) D1 EMA_MACRO slope — must align with bias
            if len(d1) >= 7:
                slope = float(last_d1["ema_macro"] - d1.iloc[-7]["ema_macro"])
            else:
                slope = 0.0

            # 3) M15 ATR floor — skip dead/chop bars
            if len(m15) >= 22:
                atr_avg = float(m15["atr"].iloc[-22:-2].mean())
                atr_now = float(last_m15["atr"])
                atr_ratio = atr_now / atr_avg if atr_avg > 0 else 1.0
            else:
                atr_ratio = 1.0
            if atr_ratio < FILTER_ATR_RATIO_MIN:
                return None

            if long_bias and h1_up and slope > FILTER_D1_SLOPE_MIN:
                return self._evaluate_long(snapshot, regime, m15, params)
            if short_bias and h1_down and slope < -FILTER_D1_SLOPE_MIN:
                return self._evaluate_short(snapshot, regime, m15, params)
            return None

        if long_bias and h1_up:
            return self._evaluate_long(snapshot, regime, m15, params)
        if short_bias and h1_down:
            return self._evaluate_short(snapshot, regime, m15, params)
        return None

    # ── Long ────────────────────────────────────────────────────────────

    def _evaluate_long(
        self,
        snapshot: MarketSnapshot,
        regime:   RegimeReading,
        m15:      pd.DataFrame,
        params:   dict,
    ) -> Optional[Candidate]:
        last = m15.iloc[-2]
        atr  = float(last["atr"])

        # Pullback to EMA_FAST: low touched / pierced it, close back above.
        touched_ema = last["low"]   <= last["ema_fast"]
        closed_back = last["close"] >  last["ema_fast"]
        bullish     = last["close"] >  last["open"]
        wick_ok     = last["lower_wick"] > 0.5 * (last["body"] + 1e-9)
        rsi_ok      = float(last["rsi"]) < RSI_OVERBOUGHT

        if not (touched_ema and closed_back and bullish and wick_ok and rsi_ok):
            return None

        swing_low = float(m15["low"].iloc[-(SWING_LOOKBACK + 2):-2].min())
        sl_price  = swing_low - params["sl_atr_buffer"] * atr
        ask       = snapshot.tick.get("ask") or float(last["close"])
        entry     = float(ask)
        risk      = entry - sl_price
        if risk <= 0:
            return None

        target_r  = params["target_r"]
        tp_price  = entry + target_r * risk
        score     = self._score(last, regime, side="long")
        if params["strict_tier_a"] and score < MIN_QUALITY_SCORE:
            return None
        tier      = "A" if score >= TIER_A_SCORE else "B"
        risk_pct  = TIER_A_RISK_PCT if tier == "A" else TIER_B_RISK_PCT

        return Candidate(
            action="BUY",
            entry_price=entry,
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            tier=tier,
            strategy=self.name,
            score=round(score, 2),
            risk_pct=risk_pct,
            rationale=(
                f"D1>EMA200, H1 fast>slow, M15 pullback to EMA{int(last['ema_fast'] * 0)} "
                f"with bullish reaction; ADX={regime.adx:.1f}; "
                f"partial_tp_R={PARTIAL_TP_R}, target_R={target_r}"
            ),
        )

    # ── Short ───────────────────────────────────────────────────────────

    def _evaluate_short(
        self,
        snapshot: MarketSnapshot,
        regime:   RegimeReading,
        m15:      pd.DataFrame,
        params:   dict,
    ) -> Optional[Candidate]:
        last = m15.iloc[-2]
        atr  = float(last["atr"])

        touched_ema = last["high"]  >= last["ema_fast"]
        closed_back = last["close"] <  last["ema_fast"]
        bearish     = last["close"] <  last["open"]
        wick_ok     = last["upper_wick"] > 0.5 * (last["body"] + 1e-9)
        rsi_ok      = float(last["rsi"]) > RSI_OVERSOLD

        if not (touched_ema and closed_back and bearish and wick_ok and rsi_ok):
            return None

        swing_high = float(m15["high"].iloc[-(SWING_LOOKBACK + 2):-2].max())
        sl_price   = swing_high + params["sl_atr_buffer"] * atr
        bid        = snapshot.tick.get("bid") or float(last["close"])
        entry      = float(bid)
        risk       = sl_price - entry
        if risk <= 0:
            return None

        target_r   = params["target_r"]
        tp_price   = entry - target_r * risk
        score      = self._score(last, regime, side="short")
        if params["strict_tier_a"] and score < MIN_QUALITY_SCORE:
            return None
        tier       = "A" if score >= TIER_A_SCORE else "B"
        risk_pct   = TIER_A_RISK_PCT if tier == "A" else TIER_B_RISK_PCT

        return Candidate(
            action="SELL",
            entry_price=entry,
            sl_price=round(sl_price, 5),
            tp_price=round(tp_price, 5),
            tier=tier,
            strategy=self.name,
            score=round(score, 2),
            risk_pct=risk_pct,
            rationale=(
                f"D1<EMA200, H1 fast<slow, M15 pullback to EMA with bearish reaction; "
                f"ADX={regime.adx:.1f}; target_R={target_r}"
            ),
        )

    # ── Scoring ─────────────────────────────────────────────────────────

    @staticmethod
    def _score(last: pd.Series, regime: RegimeReading, side: str) -> float:
        """0–10 composite quality score."""
        # ADX strength: 25→4, 40→7, 50+→10
        adx_score = max(0.0, min(10.0, (regime.adx - 20.0) * 0.5))

        # Candle conviction: bigger body relative to range = better
        body_ratio = float(last.get("body_ratio", 0.5)) or 0.0
        body_score = max(0.0, min(10.0, body_ratio * 10.0))

        # Pullback depth: ATR distance from EMA_FAST normalises this
        atr = float(last.get("atr", 0)) or 1e-9
        if side == "long":
            pull_dist = (last["ema_fast"] - last["low"]) / atr
        else:
            pull_dist = (last["high"] - last["ema_fast"]) / atr
        pull_score = max(0.0, min(10.0, pull_dist * 5.0))

        return 0.5 * adx_score + 0.3 * body_score + 0.2 * pull_score
