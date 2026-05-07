# python/backtest/data_loader.py
# ─────────────────────────────────────────────────────────────────────────────
# Historical OHLCV loader for backtests.
#
# Sources (in order of preference):
#   1. Pre-saved Parquet/CSV in data/historical/<SYMBOL>_<TF>.csv
#   2. Live MT5 pull (when terminal is running) → cached to data/historical/
#   3. Synthetic data (for tests)
#
# Files are expected with columns: time,open,high,low,close,volume
# `time` is parsed as UTC.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from config.settings import LOG_LEVEL

logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "historical"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _csv_path(symbol: str, tf: str) -> Path:
    return DATA_DIR / f"{symbol}_{tf}.csv"


def load_csv(symbol: str, tf: str) -> Optional[pd.DataFrame]:
    """Return a DataFrame indexed by UTC timestamp, or None if absent."""
    p = _csv_path(symbol, tf)
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df


def save_csv(symbol: str, tf: str, df: pd.DataFrame) -> None:
    p = _csv_path(symbol, tf)
    out = df.copy()
    if out.index.name == "time":
        out = out.reset_index()
    out.to_csv(p, index=False)
    log.info("wrote %s rows=%d", p, len(out))


def load_all(symbol: str, timeframes: list[str]) -> dict[str, pd.DataFrame]:
    """Load every TF for a symbol; raises if any TF is missing."""
    frames: dict[str, pd.DataFrame] = {}
    for tf in timeframes:
        df = load_csv(symbol, tf)
        if df is None or df.empty:
            raise FileNotFoundError(
                f"No historical data for {symbol} {tf} at {_csv_path(symbol, tf)}. "
                f"Run scripts/fetch_history.py to populate."
            )
        frames[tf] = df
    return frames
