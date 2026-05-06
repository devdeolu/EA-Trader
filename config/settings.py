# config/settings.py
# ─────────────────────────────────────────────────────────────────────────────
# All configuration lives here. Never hardcode values in core modules.
# ─────────────────────────────────────────────────────────────────────────────

# ── Broker / symbol ──────────────────────────────────────────────────────────
SYMBOL          = "EURUSD"
TIMEFRAMES      = ["M5", "M15", "H1", "H4", "D1"]   # top-down stack
PRIMARY_TF      = "M15"                               # entry timeframe
MAGIC_NUMBER    = 20260101                            # unique EA identifier

# ── ZeroMQ ports ─────────────────────────────────────────────────────────────
# Python PUBLISHES signals → MQL5 SUBSCRIBES
ZMQ_SIGNAL_PORT     = 5555     # Python → MT5  (trade signals)
ZMQ_DATA_PORT       = 5556     # MT5   → Python (trade results, account state)
ZMQ_HOST            = "localhost"

# ── Prop firm rules ──────────────────────────────────────────────────────────
# These are enforced in BOTH Python (pre-signal) and MQL5 (hard stop)
MAX_DAILY_DRAWDOWN_PCT  = 3.0   # % of account balance — daily halt
MAX_ACCOUNT_DRAWDOWN_PCT= 8.0   # % of account balance — permanent halt
MAX_TRADES_PER_DAY      = 3
MAX_CONSECUTIVE_LOSSES  = 2
EOD_CLOSE_TIME          = "23:30"   # NY time — force close all positions

# ── Risk per trade ────────────────────────────────────────────────────────────
TIER_A_RISK_PCT = 1.5   # % of account balance — high-confidence signals
TIER_B_RISK_PCT = 1.0   # % of account balance — standard signals
MIN_RR_RATIO    = 1.5   # minimum reward:risk to take any trade
MAX_SPREAD_PIPS = 1.5   # reject entry if spread exceeds this

# ── Session windows (UTC) ─────────────────────────────────────────────────────
# London: 07:00–16:00 UTC  |  NY: 12:00–21:00 UTC  |  Overlap: 12:00–16:00 UTC
SESSIONS = {
    "london":  {"start": "07:00", "end": "16:00"},
    "new_york":{"start": "12:00", "end": "21:00"},
    "overlap": {"start": "12:00", "end": "16:00"},
}
TRADE_SESSIONS = ["london", "new_york"]   # sessions where trading is allowed

# ── News blackout window (minutes each side of high-impact event) ─────────────
NEWS_BLACKOUT_MINUTES = 30

# ── Indicator parameters (starting defaults — Optuna will tune these) ─────────
ATR_PERIOD          = 14
ADX_PERIOD          = 14
EMA_FAST            = 21
EMA_SLOW            = 50
EMA_MACRO           = 200    # D1 direction filter
BB_PERIOD           = 20
BB_STD              = 2.0
REGIME_ADX_TREND    = 25.0   # ADX above this = trending regime
REGIME_ADX_RANGE    = 20.0   # ADX below this = ranging regime

# ── Partial take profit ───────────────────────────────────────────────────────
PARTIAL_TP_R        = 1.5    # close 50% at this R multiple
PARTIAL_TP_SIZE     = 0.50   # fraction of position to close
TRAIL_TO_R          = 2.5    # trail remainder to this R multiple

# ── Data / storage ────────────────────────────────────────────────────────────
import os
BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR    = os.path.join(BASE_DIR, "data")
LOG_DIR     = os.path.join(BASE_DIR, "logs")
DB_PATH     = os.path.join(DATA_DIR, "apex_trades.db")

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL   = "INFO"    # DEBUG | INFO | WARNING | ERROR
