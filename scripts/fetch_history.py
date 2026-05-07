# scripts/fetch_history.py
# ─────────────────────────────────────────────────────────────────────────────
# Pulls historical OHLCV from a running MT5 terminal and saves to
# data/historical/<SYMBOL>_<TF>.csv
#
# Usage:
#   python -m scripts.fetch_history --symbol EURUSD --tfs M5 M15 H1 H4 D1 \
#                                   --bars 50000
#
# Requires: MT5 terminal running and logged in to a broker.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import logging

from config.settings import SYMBOL, TIMEFRAMES
from python.backtest.data_loader import save_csv
from python.core.mt5_connector import MT5Connector

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=SYMBOL)
    ap.add_argument("--tfs",    nargs="+", default=TIMEFRAMES)
    ap.add_argument("--bars",   type=int, default=50_000)
    args = ap.parse_args()

    conn = MT5Connector()
    if not conn.connect():
        raise SystemExit("Could not connect to MT5 terminal")

    for tf in args.tfs:
        log.info("Fetching %s %s (%d bars)...", args.symbol, tf, args.bars)
        df = conn.get_ohlcv(args.symbol, tf, args.bars)
        if df is None or df.empty:
            log.warning("No data returned for %s %s", args.symbol, tf)
            continue
        save_csv(args.symbol, tf, df)

    conn.disconnect() if hasattr(conn, "disconnect") else None
    log.info("Done.")


if __name__ == "__main__":
    main()
