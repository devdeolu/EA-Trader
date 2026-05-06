# python/core/mt5_connector.py
# ─────────────────────────────────────────────────────────────────────────────
# Handles all communication with the MT5 terminal via the official
# MetaTrader5 Python package. Provides:
#   - OHLCV data for any symbol/timeframe
#   - Current tick (bid/ask/spread)
#   - Account state (balance, equity, drawdown)
#   - Open positions and trade history
#
# This module is READ-ONLY from the Python side.
# All order execution goes through the ZeroMQ bridge → MQL5 receiver.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import sys
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5
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
    LOG_LEVEL,
    SYMBOL,
)

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("mt5_connector")

# ── Timeframe map ─────────────────────────────────────────────────────────────
TF_MAP = {
    "M1":  mt5.TIMEFRAME_M1,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
}


class MT5Connector:
    """
    Manages the connection to a running MT5 terminal and exposes
    clean methods to pull market data and account state.
    """

    def __init__(self, symbol: str = SYMBOL):
        self.symbol = symbol
        self.connected = False

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        """Initialise connection to MT5 terminal. Returns True on success."""
        if not mt5.initialize():
            log.error(f"MT5 initialise failed: {mt5.last_error()}")
            return False

        info = mt5.terminal_info()
        if info is None:
            log.error("Could not retrieve terminal info.")
            return False

        log.info(f"Connected to MT5 | Build: {info.build} | "
                 f"Company: {info.company}")
        self.connected = True
        return True

    def disconnect(self):
        mt5.shutdown()
        self.connected = False
        log.info("MT5 connection closed.")

    def _require_connection(self):
        if not self.connected:
            raise RuntimeError("Not connected to MT5. Call connect() first.")

    # ── Market data ───────────────────────────────────────────────────────────

    def get_ohlcv(
        self,
        timeframe: str = "M15",
        bars: int = 500,
        symbol: Optional[str] = None
    ) -> pd.DataFrame:
        """
        Fetch OHLCV data as a clean DataFrame with UTC timestamps.

        Parameters
        ----------
        timeframe : str
            One of M1, M5, M15, M30, H1, H4, D1
        bars : int
            Number of candles to fetch (most recent first, then reversed)
        symbol : str, optional
            Override default symbol

        Returns
        -------
        pd.DataFrame
            Columns: open, high, low, close, volume, time (UTC index)
        """
        self._require_connection()
        sym = symbol or self.symbol
        tf  = TF_MAP.get(timeframe)
        if tf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}. "
                             f"Valid: {list(TF_MAP.keys())}")

        rates = mt5.copy_rates_from_pos(sym, tf, 0, bars)
        if rates is None or len(rates) == 0:
            log.warning(f"No data returned for {sym} {timeframe}. "
                        f"Error: {mt5.last_error()}")
            return pd.DataFrame()

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df.set_index("time", inplace=True)
        df.rename(columns={
            "open":       "open",
            "high":       "high",
            "low":        "low",
            "close":      "close",
            "tick_volume":"volume",
        }, inplace=True)
        df = df[["open", "high", "low", "close", "volume"]]
        return df

    def get_tick(self, symbol: Optional[str] = None) -> dict:
        """
        Return the current best bid/ask and spread in pips.

        Returns
        -------
        dict with keys: bid, ask, spread_pips, time
        """
        self._require_connection()
        sym  = symbol or self.symbol
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            log.warning(f"Could not retrieve tick for {sym}.")
            return {}

        info        = mt5.symbol_info(sym)
        point       = info.point if info else 0.00001
        pip_size    = point * 10

        return {
            "bid":         tick.bid,
            "ask":         tick.ask,
            "spread_pips": round((tick.ask - tick.bid) / pip_size, 1),
            "time":        datetime.fromtimestamp(tick.time, tz=timezone.utc),
        }

    # ── Account state ─────────────────────────────────────────────────────────

    def get_account_state(self) -> dict:
        """
        Return current account metrics relevant to prop firm rule checking.

        Returns
        -------
        dict with keys: balance, equity, margin, free_margin,
                        drawdown_pct, profit_today
        """
        self._require_connection()
        acc = mt5.account_info()
        if acc is None:
            log.warning("Could not retrieve account info.")
            return {}

        drawdown_pct = (
            round((acc.balance - acc.equity) / acc.balance * 100, 2)
            if acc.balance > 0 else 0.0
        )

        return {
            "balance":      acc.balance,
            "equity":       acc.equity,
            "margin":       acc.margin,
            "free_margin":  acc.margin_free,
            "drawdown_pct": drawdown_pct,
            "profit_today": round(acc.equity - acc.balance, 2),
            "leverage":     acc.leverage,
            "currency":     acc.currency,
        }

    def get_open_positions(self, symbol: Optional[str] = None) -> pd.DataFrame:
        """Return all open positions as a DataFrame."""
        self._require_connection()
        sym       = symbol or self.symbol
        positions = mt5.positions_get(symbol=sym)
        if not positions:
            return pd.DataFrame()

        rows = []
        for p in positions:
            rows.append({
                "ticket":     p.ticket,
                "symbol":     p.symbol,
                "type":       "BUY" if p.type == 0 else "SELL",
                "volume":     p.volume,
                "open_price": p.price_open,
                "sl":         p.sl,
                "tp":         p.tp,
                "profit":     p.profit,
                "magic":      p.magic,
                "comment":    p.comment,
                "open_time":  datetime.fromtimestamp(p.time, tz=timezone.utc),
            })
        return pd.DataFrame(rows)

    def get_today_trades(self, symbol: Optional[str] = None) -> pd.DataFrame:
        """
        Return all closed deals from today (UTC midnight to now).
        Used by the prop firm rule checker to count daily trades,
        consecutive losses, and daily P&L.
        """
        self._require_connection()
        sym        = symbol or self.symbol
        today_utc  = datetime.now(timezone.utc).replace(
                        hour=0, minute=0, second=0, microsecond=0)
        now_utc    = datetime.now(timezone.utc)

        deals = mt5.history_deals_get(today_utc, now_utc, symbol=sym)
        if not deals:
            return pd.DataFrame()

        rows = []
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_OUT:    # only closing deals
                rows.append({
                    "ticket":      d.ticket,
                    "order":       d.order,
                    "symbol":      d.symbol,
                    "type":        "BUY" if d.type == 0 else "SELL",
                    "volume":      d.volume,
                    "price":       d.price,
                    "profit":      d.profit,
                    "commission":  d.commission,
                    "swap":        d.swap,
                    "net_profit":  round(d.profit + d.commission + d.swap, 2),
                    "time":        datetime.fromtimestamp(d.time, tz=timezone.utc),
                    "comment":     d.comment,
                })
        return pd.DataFrame(rows)

    # ── Symbol info ───────────────────────────────────────────────────────────

    def get_symbol_info(self, symbol: Optional[str] = None) -> dict:
        """Return key symbol properties needed for lot size calculation."""
        self._require_connection()
        sym  = symbol or self.symbol
        info = mt5.symbol_info(sym)
        if info is None:
            return {}
        return {
            "symbol":         info.name,
            "digits":         info.digits,
            "point":          info.point,
            "pip_size":       info.point * 10,
            "contract_size":  info.trade_contract_size,
            "min_lot":        info.volume_min,
            "max_lot":        info.volume_max,
            "lot_step":       info.volume_step,
        }


# ── Indicator engine ──────────────────────────────────────────────────────────
# Pure pandas/numpy — no TA-Lib dependency for Phase 1.
# TA-Lib versions are added in Phase 2 for speed once validated.

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all indicators needed by the regime classifier and
    strategy modules. Operates in-place on a copy of df.

    Adds columns:
        atr, adx, ema_fast, ema_slow, ema_macro (if D1 bars present),
        bb_upper, bb_mid, bb_lower, bb_width, rsi
    """
    d = df.copy()

    # ── ATR ──────────────────────────────────────────────────────────────────
    high_low   = d["high"] - d["low"]
    high_close = (d["high"] - d["close"].shift(1)).abs()
    low_close  = (d["low"]  - d["close"].shift(1)).abs()
    tr         = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    d["atr"]   = tr.ewm(span=ATR_PERIOD, adjust=False).mean()

    # ── EMA ───────────────────────────────────────────────────────────────────
    d["ema_fast"] = d["close"].ewm(span=EMA_FAST, adjust=False).mean()
    d["ema_slow"] = d["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    d["ema_macro"]= d["close"].ewm(span=EMA_MACRO, adjust=False).mean()

    # ── ADX ───────────────────────────────────────────────────────────────────
    up_move   = d["high"].diff()
    down_move = -d["low"].diff()
    plus_dm   = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm  = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr_s     = pd.Series(tr).ewm(span=ADX_PERIOD, adjust=False).mean()
    plus_di   = 100 * pd.Series(plus_dm).ewm(
                    span=ADX_PERIOD, adjust=False).mean() / atr_s
    minus_di  = 100 * pd.Series(minus_dm).ewm(
                    span=ADX_PERIOD, adjust=False).mean() / atr_s

    dx        = (100 * (plus_di - minus_di).abs() /
                 (plus_di + minus_di).replace(0, np.nan))
    d["adx"]      = dx.ewm(span=ADX_PERIOD, adjust=False).mean()
    d["plus_di"]  = plus_di.values
    d["minus_di"] = minus_di.values

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    d["bb_mid"]   = d["close"].rolling(BB_PERIOD).mean()
    bb_std        = d["close"].rolling(BB_PERIOD).std()
    d["bb_upper"] = d["bb_mid"] + BB_STD * bb_std
    d["bb_lower"] = d["bb_mid"] - BB_STD * bb_std
    d["bb_width"] = (d["bb_upper"] - d["bb_lower"]) / d["bb_mid"]

    # ── RSI ───────────────────────────────────────────────────────────────────
    delta     = d["close"].diff()
    gain      = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss      = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    rs        = gain / loss.replace(0, np.nan)
    d["rsi"]  = 100 - (100 / (1 + rs))

    # ── Candle body / wick ratios (useful for entry scoring) ─────────────────
    d["body"]        = (d["close"] - d["open"]).abs()
    d["upper_wick"]  = d["high"] - d[["open", "close"]].max(axis=1)
    d["lower_wick"]  = d[["open", "close"]].min(axis=1) - d["low"]
    d["body_ratio"]  = d["body"] / (d["high"] - d["low"]).replace(0, np.nan)

    return d.dropna(subset=["atr", "adx", "rsi"])


# ── Quick sanity test ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing MT5 connector...")
    conn = MT5Connector()

    if not conn.connect():
        print("Could not connect. Is MT5 running?")
        sys.exit(1)

    print("\n── Account state ───────────────────────────────")
    state = conn.get_account_state()
    for k, v in state.items():
        print(f"  {k:20s}: {v}")

    print("\n── Current tick ────────────────────────────────")
    tick = conn.get_tick()
    for k, v in tick.items():
        print(f"  {k:20s}: {v}")

    print("\n── OHLCV M15 (last 5 bars) ─────────────────────")
    df = conn.get_ohlcv("M15", bars=200)
    df = add_indicators(df)
    print(df[["open","high","low","close","atr","adx","rsi"]].tail(5).to_string())

    print("\n── Symbol info ─────────────────────────────────")
    info = conn.get_symbol_info()
    for k, v in info.items():
        print(f"  {k:20s}: {v}")

    print("\n── Today's closed trades ───────────────────────")
    trades = conn.get_today_trades()
    if trades.empty:
        print("  No closed trades today.")
    else:
        print(trades[["type","profit","net_profit","time"]].to_string())

    conn.disconnect()
    print("\nPhase 1 MT5 connector: OK")
