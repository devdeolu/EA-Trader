# python/core/data_engine.py
# ─────────────────────────────────────────────────────────────────────────────
# Multi-timeframe feature pipeline.
#
# Pulls OHLCV from MT5 across all timeframes in settings.TIMEFRAMES, computes
# indicators (via mt5_connector.add_indicators), and exposes a unified snapshot
# that strategy modules consume.
#
# Design principles:
#   - Pure function style: get_snapshot(connector) → dict of DataFrames + scalar
#     summary. No hidden state, easy to unit test with mocked connectors.
#   - All timestamps are UTC. Strategies never see broker-local time.
#   - Cheap to call on every tick: caches per-bar so repeat calls within the
#     same bar don't refetch.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import pandas as pd

from config.settings import LOG_LEVEL, PRIMARY_TF, SYMBOL, TIMEFRAMES
from python.core.indicators import add_indicators

if TYPE_CHECKING:  # only imported for typing — avoids MT5 SDK dep at import time
    from python.core.mt5_connector import MT5Connector

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("data_engine")


# ── Snapshot container ──────────────────────────────────────────────────────

@dataclass
class MarketSnapshot:
    """
    Frozen view of the market at a point in time. Passed to strategies, the
    regime classifier, and the risk gate.
    """
    symbol:        str
    taken_at:      datetime
    primary_tf:    str
    frames:        dict[str, pd.DataFrame] = field(default_factory=dict)
    tick:          dict = field(default_factory=dict)
    account:       dict = field(default_factory=dict)
    symbol_info:   dict = field(default_factory=dict)

    # ── Convenience accessors ────────────────────────────────────────────
    def primary(self) -> pd.DataFrame:
        return self.frames[self.primary_tf]

    def latest(self, tf: Optional[str] = None) -> pd.Series:
        """Most recent fully-formed bar on the given timeframe."""
        df = self.frames[tf or self.primary_tf]
        # second-to-last = last closed bar (last row may still be forming)
        return df.iloc[-2]

    def forming(self, tf: Optional[str] = None) -> pd.Series:
        """Currently-forming bar (last row)."""
        return self.frames[tf or self.primary_tf].iloc[-1]


# ── Feature pipeline ────────────────────────────────────────────────────────

class DataEngine:
    """
    Pulls all needed market data and produces a MarketSnapshot.

    Per-bar caching keeps the cost of repeat calls within the same M1 bar
    negligible — the tick loop can call get_snapshot() freely.
    """

    def __init__(
        self,
        connector:  "MT5Connector",
        symbol:     str = SYMBOL,
        timeframes: list[str] = None,
        primary_tf: str = PRIMARY_TF,
        bars_per_tf:int = 500,
    ):
        self.connector   = connector
        self.symbol      = symbol
        self.timeframes  = timeframes or TIMEFRAMES
        self.primary_tf  = primary_tf
        self.bars_per_tf = bars_per_tf

        # ── cache: timeframe → (last_bar_time, indicator_df) ─────────────
        self._cache: dict[str, tuple[pd.Timestamp, pd.DataFrame]] = {}

    # ── Public API ─────────────────────────────────────────────────────

    def get_snapshot(self) -> MarketSnapshot:
        frames = {tf: self._get_frame(tf) for tf in self.timeframes}

        return MarketSnapshot(
            symbol      = self.symbol,
            taken_at    = datetime.now(timezone.utc),
            primary_tf  = self.primary_tf,
            frames      = frames,
            tick        = self.connector.get_tick(self.symbol),
            account     = self.connector.get_account_state(),
            symbol_info = self.connector.get_symbol_info(self.symbol),
        )

    # ── Internals ───────────────────────────────────────────────────────

    def _get_frame(self, tf: str) -> pd.DataFrame:
        """
        Return indicator-augmented OHLCV for `tf`, using the cache when the
        latest closed bar hasn't advanced.
        """
        df_raw = self.connector.get_ohlcv(
            timeframe=tf, bars=self.bars_per_tf, symbol=self.symbol
        )
        if df_raw.empty:
            log.warning("Empty OHLCV for %s %s", self.symbol, tf)
            return df_raw

        last_closed = df_raw.index[-2] if len(df_raw) >= 2 else df_raw.index[-1]

        cached = self._cache.get(tf)
        if cached and cached[0] == last_closed:
            # Same bar — only the forming candle changed. Recompute indicators
            # cheaply by replacing the last row of cached frame.
            cached_df = cached[1]
            new_last  = df_raw.iloc[-1:].copy()
            stitched  = pd.concat([cached_df.iloc[:-1], new_last])
            # Re-run indicators only on the tail to keep it fast.
            return add_indicators(stitched.tail(self.bars_per_tf))

        df = add_indicators(df_raw)
        self._cache[tf] = (last_closed, df)
        return df


# ── Quick test ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Testing data engine (requires running MT5 terminal)...")
    from python.core.mt5_connector import MT5Connector
    conn = MT5Connector()
    if not conn.connect():
        print("  Could not connect to MT5.")
        raise SystemExit(1)

    engine = DataEngine(conn)
    snap   = engine.get_snapshot()

    print(f"\n  Symbol      : {snap.symbol}")
    print(f"  Taken at    : {snap.taken_at}")
    print(f"  Primary TF  : {snap.primary_tf}")
    print(f"  Frames      : {list(snap.frames.keys())}")
    for tf, df in snap.frames.items():
        if df.empty:
            print(f"    {tf:5s}: EMPTY")
            continue
        last = df.iloc[-2]
        print(f"    {tf:5s}: bars={len(df)} | "
              f"close={last['close']:.5f} | "
              f"atr={last['atr']:.5f} | adx={last['adx']:.1f}")

    print(f"\n  Tick        : {snap.tick}")
    print(f"  Account     : {snap.account}")

    conn.disconnect()
    print("\nData engine: OK")
