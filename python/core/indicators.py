# python/core/indicators.py
# ─────────────────────────────────────────────────────────────────────────────
# Pure-pandas indicator pipeline. NO MT5 dependency — safe to import in
# backtests, tests, and notebooks without a running terminal.
#
# Replace internals with TA-Lib later for speed; the public API
# (add_indicators(df) -> df) stays identical.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import numpy as np
import pandas as pd

from config.settings import (
    ADX_PERIOD,
    ATR_PERIOD,
    BB_PERIOD,
    BB_STD,
    EMA_FAST,
    EMA_MACRO,
    EMA_SLOW,
)


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all indicators needed by the regime classifier and strategies.

    Adds columns:
        atr, adx, plus_di, minus_di,
        ema_fast, ema_slow, ema_macro,
        bb_upper, bb_mid, bb_lower, bb_width,
        rsi,
        body, upper_wick, lower_wick, body_ratio
    """
    d = df.copy()

    # ── ATR ──────────────────────────────────────────────────────────────
    high_low   = d["high"] - d["low"]
    high_close = (d["high"] - d["close"].shift(1)).abs()
    low_close  = (d["low"]  - d["close"].shift(1)).abs()
    tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    d["atr"]   = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # ── EMA ──────────────────────────────────────────────────────────────
    d["ema_fast"]  = d["close"].ewm(span=EMA_FAST,  adjust=False).mean()
    d["ema_slow"]  = d["close"].ewm(span=EMA_SLOW,  adjust=False).mean()
    d["ema_macro"] = d["close"].ewm(span=EMA_MACRO, adjust=False).mean()

    # ── ADX ──────────────────────────────────────────────────────────────
    up_move   = d["high"].diff()
    down_move = -d["low"].diff()
    plus_dm   = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=d.index,
    )
    minus_dm  = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=d.index,
    )

    atr_s    = tr.ewm(span=ADX_PERIOD, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr_s
    minus_di = 100 * minus_dm.ewm(span=ADX_PERIOD, adjust=False).mean() / atr_s
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)

    d["adx"]      = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    d["plus_di"]  = plus_di
    d["minus_di"] = minus_di

    # ── Bollinger ────────────────────────────────────────────────────────
    d["bb_mid"]   = d["close"].rolling(BB_PERIOD).mean()
    bb_std        = d["close"].rolling(BB_PERIOD).std()
    d["bb_upper"] = d["bb_mid"] + BB_STD * bb_std
    d["bb_lower"] = d["bb_mid"] - BB_STD * bb_std
    d["bb_width"] = (d["bb_upper"] - d["bb_lower"]) / d["bb_mid"]

    # ── RSI ──────────────────────────────────────────────────────────────
    delta = d["close"].diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    d["rsi"] = 100 - (100 / (1 + rs))

    # ── Candle structure ─────────────────────────────────────────────────
    d["body"]       = (d["close"] - d["open"]).abs()
    d["upper_wick"] = d["high"] - d[["open", "close"]].max(axis=1)
    d["lower_wick"] = d[["open", "close"]].min(axis=1) - d["low"]
    d["body_ratio"] = d["body"] / (d["high"] - d["low"]).replace(0, np.nan)

    return d.dropna(subset=["atr", "adx", "rsi"])
