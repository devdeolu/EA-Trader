# main.py
# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 orchestrator — wires the components and runs the live tick loop.
#
# Responsibilities:
#   - Load .env
#   - Connect MT5
#   - Spin up data engine, regime classifier, news guard, risk gate
#   - Start ZMQ bridge (publisher + result subscriber + heartbeat)
#   - On each tick: build snapshot → classify regime → strategies (Phase 2)
#     → risk gate → publish signal
#
# Phase 1 stops short of running any strategy. It validates the whole pipeline
# end-to-end with a NO-OP candidate signal so you can confirm the bridge,
# data flow, and gate logic before adding strategy modules in Phase 2.
#
# Run:
#     python -m main          # from project root
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # noqa: E402  — must run before importing settings

from config.settings import LOG_LEVEL, PRIMARY_TF, SYMBOL  # noqa: E402
from python.core.data_engine import DataEngine  # noqa: E402
from python.core.mt5_connector import MT5Connector  # noqa: E402
from python.core.regime import classify  # noqa: E402
from python.core.risk import RiskGate  # noqa: E402
from python.core.zmq_bridge import ZMQBridge  # noqa: E402
from python.utils.logger import TradeLogger  # noqa: E402
from python.utils.news_guard import NewsGuard  # noqa: E402
from python.utils.notifier import TelegramNotifier  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("apex.main")

TICK_INTERVAL_SEC = 5     # how often to pull a snapshot in Phase 1


class ApexEngine:
    def __init__(self):
        self.connector = MT5Connector(symbol=SYMBOL)
        self.bridge:    ZMQBridge | None    = None
        self.engine:    DataEngine | None   = None
        self.news:      NewsGuard | None    = None
        self.gate:      RiskGate | None     = None
        self.logger:    TradeLogger | None  = None
        self.notifier:  TelegramNotifier    = TelegramNotifier()
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> bool:
        log.info("Apex starting | symbol=%s | primary_tf=%s", SYMBOL, PRIMARY_TF)

        if not self.connector.connect():
            log.error("MT5 connection failed — aborting.")
            return False

        self.logger = TradeLogger()
        self.engine = DataEngine(self.connector)

        self.news = NewsGuard()
        try:
            self.news.load()
        except Exception as e:
            log.warning("News guard load failed: %s — continuing without it", e)
            self.news = None

        self.gate   = RiskGate(news=self.news)
        self.bridge = ZMQBridge(logger=self.logger)
        self.bridge.start()

        if self.notifier.enabled:
            self.notifier.send(f"🟢 Apex EA started — {SYMBOL}")

        self._running = True
        return True

    def stop(self):
        log.info("Apex stopping...")
        self._running = False
        if self.bridge:
            self.bridge.stop()
        if self.connector:
            self.connector.disconnect()
        if self.notifier.enabled:
            self.notifier.send("🔴 Apex EA stopped")

    # ── Tick loop ──────────────────────────────────────────────────────

    def run_forever(self):
        last_minute_logged = -1
        while self._running:
            try:
                self._tick()
            except Exception as e:
                log.exception("Tick loop error: %s", e)
                if self.notifier.enabled:
                    self.notifier.send(f"⚠️ Apex tick error: {e!s:.200}")
            # Heartbeat-style minute marker so we can see the loop is alive
            now = datetime.now(timezone.utc)
            if now.minute != last_minute_logged:
                log.info("alive | %s UTC", now.strftime("%H:%M"))
                last_minute_logged = now.minute
            time.sleep(TICK_INTERVAL_SEC)

    def _tick(self):
        snapshot = self.engine.get_snapshot()
        if snapshot.frames[snapshot.primary_tf].empty:
            log.debug("No data yet — skipping tick")
            return

        regime = classify(snapshot)
        log.debug(
            "regime=%s | adx=%.1f | atr_ratio=%.2f | spread=%s",
            regime.regime.value,
            regime.adx,
            regime.atr_ratio,
            snapshot.tick.get("spread_pips"),
        )

        # Phase 2 hook: iterate registered strategies and produce candidate
        # signals here, then run each through self.gate.check(...) before
        # bridge.send(...). Phase 1 is observation-only.


# ── Entry point ────────────────────────────────────────────────────────────

def _install_signal_handlers(app: ApexEngine):
    def handler(signum, _frame):
        log.info("Received signal %s — shutting down", signum)
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handler)


def main():
    app = ApexEngine()
    _install_signal_handlers(app)
    if not app.start():
        sys.exit(1)
    try:
        app.run_forever()
    finally:
        app.stop()


if __name__ == "__main__":
    main()
