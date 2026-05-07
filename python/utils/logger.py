# python/utils/logger.py
# ─────────────────────────────────────────────────────────────────────────────
# Structured trade logger backed by SQLite.
# Every closed trade is stored with full metadata so the evolution engine
# can query performance by strategy, regime, session, and time period.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from config.settings import DB_PATH, LOG_DIR, LOG_LEVEL

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("trade_logger")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


# ── Schema ────────────────────────────────────────────────────────────────────

CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket          INTEGER UNIQUE,
    symbol          TEXT    NOT NULL,
    direction       TEXT    NOT NULL,    -- BUY | SELL
    lots            REAL    NOT NULL,
    open_price      REAL,
    close_price     REAL,
    sl_price        REAL,
    tp_price        REAL,
    gross_profit    REAL,
    commission      REAL,
    swap            REAL,
    net_profit      REAL,
    r_multiple      REAL,               -- actual R achieved
    open_time       TEXT,
    close_time      TEXT,
    duration_mins   INTEGER,
    strategy        TEXT,               -- trend_pullback | mean_reversion
    tier            TEXT,               -- A | B
    regime          TEXT,               -- trending | ranging | volatile
    session         TEXT,               -- london | new_york | overlap
    adx_at_entry    REAL,
    atr_at_entry    REAL,
    rsi_at_entry    REAL,
    entry_score     REAL,               -- 0-10 signal quality score
    exit_reason     TEXT,               -- tp1 | tp2 | sl | manual | eod
    magic           INTEGER,
    comment         TEXT,
    logged_at       TEXT DEFAULT (datetime('now','utc'))
);
"""

CREATE_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS daily_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    UNIQUE,
    trades_taken    INTEGER DEFAULT 0,
    wins            INTEGER DEFAULT 0,
    losses          INTEGER DEFAULT 0,
    gross_profit    REAL    DEFAULT 0,
    net_profit      REAL    DEFAULT 0,
    max_drawdown    REAL    DEFAULT 0,
    starting_balance REAL,
    ending_equity   REAL,
    logged_at       TEXT DEFAULT (datetime('now','utc'))
);
"""

CREATE_SIGNALS_TABLE = """
CREATE TABLE IF NOT EXISTS signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT,
    action      TEXT,
    strategy    TEXT,
    regime      TEXT,
    session     TEXT,
    tier        TEXT,
    lots        REAL,
    sl_price    REAL,
    tp_price    REAL,
    sent        INTEGER DEFAULT 1,      -- 1 = sent to MT5, 0 = filtered
    filter_reason TEXT,
    ts          TEXT,
    logged_at   TEXT DEFAULT (datetime('now','utc'))
);
"""


class TradeLogger:
    """
    SQLite-backed logger for trades, daily stats, and signal history.

    Usage:
        logger = TradeLogger()
        logger.log_trade(result_dict)   # called by ZMQ result subscriber
        logger.log_signal(signal_dict)  # called before every signal send

        # Query performance
        df = logger.get_stats(strategy="trend_pullback", regime="trending")
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    # ── Init ──────────────────────────────────────────────────────────────────

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(CREATE_TRADES_TABLE)
            conn.execute(CREATE_STATS_TABLE)
            conn.execute(CREATE_SIGNALS_TABLE)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy "
                         "ON trades(strategy)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_regime "
                         "ON trades(regime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_close_time "
                         "ON trades(close_time)")
        log.info(f"Trade database ready: {self.db_path}")

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # ── Log trade ─────────────────────────────────────────────────────────────

    def log_trade(self, result: dict, metadata: Optional[dict] = None):
        """
        Log a closed trade from the MT5 result payload.
        `metadata` can supply strategy/regime/session/entry context
        if not present in the result dict.
        """
        meta = metadata or {}

        open_time  = result.get("open_time")
        close_time = result.get("close_time")
        duration   = None
        if open_time and close_time:
            try:
                ot = datetime.fromisoformat(open_time)
                ct = datetime.fromisoformat(close_time)
                duration = int((ct - ot).total_seconds() / 60)
            except Exception:
                pass

        # R-multiple: net_profit / (risk in $)
        # Risk $ = lots * contract_size * SL_pips * pip_value
        # Simplified: store None if we can't calculate cleanly
        r_multiple = meta.get("r_multiple")

        row = {
            "ticket":       result.get("ticket"),
            "symbol":       result.get("symbol", "EURUSD"),
            "direction":    result.get("type", ""),
            "lots":         result.get("volume", 0),
            "open_price":   result.get("open_price"),
            "close_price":  result.get("close_price"),
            "sl_price":     meta.get("sl_price"),
            "tp_price":     meta.get("tp_price"),
            "gross_profit": result.get("profit", 0),
            "commission":   result.get("commission", 0),
            "swap":         result.get("swap", 0),
            "net_profit":   result.get("net_profit", 0),
            "r_multiple":   r_multiple,
            "open_time":    open_time,
            "close_time":   close_time,
            "duration_mins":duration,
            "strategy":     meta.get("strategy", result.get("comment", "")),
            "tier":         meta.get("tier", ""),
            "regime":       meta.get("regime", ""),
            "session":      meta.get("session", ""),
            "adx_at_entry": meta.get("adx_at_entry"),
            "atr_at_entry": meta.get("atr_at_entry"),
            "rsi_at_entry": meta.get("rsi_at_entry"),
            "entry_score":  meta.get("entry_score"),
            "exit_reason":  result.get("comment", ""),
            "magic":        result.get("magic"),
            "comment":      result.get("comment", ""),
        }

        sql = """
            INSERT OR REPLACE INTO trades
            (ticket, symbol, direction, lots, open_price, close_price,
             sl_price, tp_price, gross_profit, commission, swap, net_profit,
             r_multiple, open_time, close_time, duration_mins,
             strategy, tier, regime, session,
             adx_at_entry, atr_at_entry, rsi_at_entry, entry_score,
             exit_reason, magic, comment)
            VALUES
            (:ticket,:symbol,:direction,:lots,:open_price,:close_price,
             :sl_price,:tp_price,:gross_profit,:commission,:swap,:net_profit,
             :r_multiple,:open_time,:close_time,:duration_mins,
             :strategy,:tier,:regime,:session,
             :adx_at_entry,:atr_at_entry,:rsi_at_entry,:entry_score,
             :exit_reason,:magic,:comment)
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, row)
            log.info(f"Trade logged | ticket={row['ticket']} | "
                     f"net_profit={row['net_profit']}")
        except Exception as e:
            log.error(f"Failed to log trade: {e} | data={row}")

    def log_signal(self, signal: dict, sent: bool = True,
                   filter_reason: str = ""):
        """Log every signal generated — whether sent or filtered out."""
        row = {
            "symbol":        signal.get("symbol"),
            "action":        signal.get("action"),
            "strategy":      signal.get("strategy"),
            "regime":        signal.get("regime"),
            "session":       signal.get("session"),
            "tier":          signal.get("tier"),
            "lots":          signal.get("lots"),
            "sl_price":      signal.get("sl_price"),
            "tp_price":      signal.get("tp_price"),
            "sent":          1 if sent else 0,
            "filter_reason": filter_reason,
            "ts":            signal.get("ts"),
        }
        sql = """
            INSERT INTO signals
            (symbol,action,strategy,regime,session,tier,lots,
             sl_price,tp_price,sent,filter_reason,ts)
            VALUES
            (:symbol,:action,:strategy,:regime,:session,:tier,:lots,
             :sl_price,:tp_price,:sent,:filter_reason,:ts)
        """
        try:
            with self._conn() as conn:
                conn.execute(sql, row)
        except Exception as e:
            log.error(f"Failed to log signal: {e}")

    # ── Query helpers (for evolution engine in Phase 3+) ─────────────────────

    def get_stats(
        self,
        strategy: Optional[str]  = None,
        regime:   Optional[str]  = None,
        session:  Optional[str]  = None,
        last_n:   Optional[int]  = None,
    ) -> dict:
        """
        Return performance stats filtered by strategy/regime/session.
        Used by the evolution engine to evaluate parameter quality.
        """
        conditions = []
        params     = []

        if strategy:
            conditions.append("strategy = ?")
            params.append(strategy)
        if regime:
            conditions.append("regime = ?")
            params.append(regime)
        if session:
            conditions.append("session = ?")
            params.append(session)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit = f"LIMIT {last_n}" if last_n else ""

        sql = f"""
            SELECT
                COUNT(*)                        AS total_trades,
                SUM(CASE WHEN net_profit > 0
                    THEN 1 ELSE 0 END)          AS wins,
                SUM(CASE WHEN net_profit <= 0
                    THEN 1 ELSE 0 END)          AS losses,
                AVG(net_profit)                 AS avg_net,
                SUM(net_profit)                 AS total_net,
                SUM(CASE WHEN net_profit > 0
                    THEN net_profit ELSE 0 END) AS gross_win,
                SUM(CASE WHEN net_profit <= 0
                    THEN ABS(net_profit) ELSE 0 END) AS gross_loss,
                AVG(r_multiple)                 AS avg_r,
                AVG(duration_mins)              AS avg_duration_mins
            FROM (
                SELECT * FROM trades {where}
                ORDER BY close_time DESC {limit}
            )
        """
        try:
            with self._conn() as conn:
                row = conn.execute(sql, params).fetchone()
            if not row or row[0] == 0:
                return {"total_trades": 0}

            total, wins, losses, avg_net, total_net, \
                gw, gl, avg_r, avg_dur = row

            win_rate     = round(wins / total * 100, 1) if total else 0
            profit_factor= round(gw / gl, 2) if gl and gl > 0 else None

            return {
                "total_trades":   total,
                "wins":           wins,
                "losses":         losses,
                "win_rate_pct":   win_rate,
                "profit_factor":  profit_factor,
                "avg_net_profit": round(avg_net or 0, 2),
                "total_net":      round(total_net or 0, 2),
                "avg_r":          round(avg_r or 0, 2),
                "avg_duration_mins": round(avg_dur or 0, 1),
                "filter": {
                    "strategy": strategy,
                    "regime":   regime,
                    "session":  session,
                    "last_n":   last_n,
                }
            }
        except Exception as e:
            log.error(f"Stats query failed: {e}")
            return {}

    def get_daily_summary(self, date_str: Optional[str] = None) -> dict:
        """Return today's trade summary for the prop firm rule checker."""
        date = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sql  = """
            SELECT
                COUNT(*)                AS trades,
                SUM(net_profit)         AS net_profit,
                SUM(CASE WHEN net_profit <= 0 THEN 1 ELSE 0 END) AS losses,
                MIN(net_profit)         AS worst_trade
            FROM trades
            WHERE DATE(close_time) = ?
        """
        with self._conn() as conn:
            row = conn.execute(sql, (date,)).fetchone()
        if not row:
            return {"date": date, "trades": 0}
        return {
            "date":        date,
            "trades":      row[0] or 0,
            "net_profit":  round(row[1] or 0, 2),
            "losses":      row[2] or 0,
            "worst_trade": round(row[3] or 0, 2),
        }

    def get_consecutive_losses(self) -> int:
        """
        Return the length of the current trailing run of losing trades
        (most recent first). Used by the live risk gate.
        """
        sql = "SELECT net_profit FROM trades ORDER BY close_time DESC LIMIT 50"
        try:
            with self._conn() as conn:
                rows = conn.execute(sql).fetchall()
        except Exception as e:
            log.error(f"Consecutive loss query failed: {e}")
            return 0

        run = 0
        for (net,) in rows:
            if net is None:
                break
            if net <= 0:
                run += 1
            else:
                break
        return run


# ── Quick test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import tempfile, os
    test_db = os.path.join(tempfile.gettempdir(), "apex_test.db")
    logger  = TradeLogger(db_path=test_db)

    fake_result = {
        "event":       "TRADE_CLOSED",
        "ticket":      99001,
        "symbol":      "EURUSD",
        "type":        "BUY",
        "volume":      0.02,
        "open_price":  1.08450,
        "close_price": 1.08620,
        "profit":      34.00,
        "commission":  -0.70,
        "swap":        0.00,
        "net_profit":  33.30,
        "open_time":   "2026-01-15T09:32:00+00:00",
        "close_time":  "2026-01-15T11:14:00+00:00",
        "magic":       20260101,
        "comment":     "tp1",
    }
    fake_meta = {
        "strategy":     "trend_pullback",
        "tier":         "A",
        "regime":       "trending",
        "session":      "london",
        "adx_at_entry": 32.4,
        "atr_at_entry": 0.00085,
        "rsi_at_entry": 54.2,
        "entry_score":  8.2,
        "sl_price":     1.08300,
        "tp_price":     1.08700,
        "r_multiple":   1.75,
    }

    logger.log_trade(fake_result, fake_meta)

    stats = logger.get_stats(strategy="trend_pullback")
    print("\nStats for trend_pullback:")
    for k, v in stats.items():
        print(f"  {k:25s}: {v}")

    daily = logger.get_daily_summary("2026-01-15")
    print("\nDaily summary 2026-01-15:")
    for k, v in daily.items():
        print(f"  {k:25s}: {v}")

    os.unlink(test_db)
    print("\nTrade logger: OK")
