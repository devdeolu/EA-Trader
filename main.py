# main.py
# ─────────────────────────────────────────────────────────────────────────────
# Apex EA orchestrator.
#
# Wires:
#   load .env → MT5 → DataEngine → NewsGuard → Strategies → RiskGate
#                                            → Sizing → ZMQ bridge → MQL5
#
# The tick loop:
#   - Polls every TICK_INTERVAL_SEC.
#   - Only evaluates strategies on a NEW closed primary-TF bar (one entry
#     attempt per bar, per strategy) — prevents repeated signals while a
#     setup persists.
#   - Logs every candidate (sent OR filtered) to the SQLite signal log so
#     the evolution engine can later analyse miss reasons.
#
# Run:
#     python -m main
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import signal
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()  # noqa: E402  — must run before importing settings

from config.settings import (  # noqa: E402
    LOG_LEVEL,
    MAGIC_NUMBER,
    PRIMARY_TF,
    SYMBOL,
)
from python.core.data_engine import DataEngine, MarketSnapshot  # noqa: E402
from python.core.mt5_connector import MT5Connector  # noqa: E402
from python.core.regime import RegimeReading, classify  # noqa: E402
from python.core.risk import RiskGate  # noqa: E402
from python.core.sizing import compute_lots  # noqa: E402
from python.core.zmq_bridge import ZMQBridge, build_signal  # noqa: E402
from python.strategies.base import Candidate, Strategy  # noqa: E402
from python.strategies.trend_pullback import TrendPullback  # noqa: E402
from python.utils.logger import TradeLogger  # noqa: E402
from python.utils.news_guard import NewsGuard  # noqa: E402
from python.utils.notifier import TelegramNotifier  # noqa: E402

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-7s | %(name)-15s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("apex.main")

TICK_INTERVAL_SEC = 5


# ── Strategy registry ─────────────────────────────────────────────────────
def _default_strategies() -> list[Strategy]:
    return [TrendPullback()]


class ApexEngine:
    def __init__(self, strategies: list[Strategy] | None = None):
        self.connector  = MT5Connector(symbol=SYMBOL)
        self.strategies = strategies or _default_strategies()
        self.bridge:   ZMQBridge | None    = None
        self.engine:   DataEngine | None   = None
        self.news:     NewsGuard | None    = None
        self.gate:     RiskGate | None     = None
        self.logger:   TradeLogger | None  = None
        self.notifier: TelegramNotifier    = TelegramNotifier()

        # bar-edge dedup: (strategy_name, primary_tf_bar_time) we've evaluated
        self._evaluated_bars: set[tuple[str, str]] = set()
        self._last_bar_time: datetime | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> bool:
        log.info(
            "Apex starting | symbol=%s | primary_tf=%s | strategies=%s",
            SYMBOL, PRIMARY_TF, [s.name for s in self.strategies],
        )

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

            now = datetime.now(timezone.utc)
            if now.minute != last_minute_logged:
                log.info("alive | %s UTC", now.strftime("%H:%M"))
                last_minute_logged = now.minute
            time.sleep(TICK_INTERVAL_SEC)

    def _tick(self):
        snapshot = self.engine.get_snapshot()
        primary  = snapshot.frames.get(snapshot.primary_tf)
        if primary is None or primary.empty or len(primary) < 3:
            return

        # ── Only act on a brand-new closed primary-TF bar ───────────────
        last_closed_time = primary.index[-2].to_pydatetime()
        if last_closed_time == self._last_bar_time:
            return
        self._last_bar_time = last_closed_time

        regime = classify(snapshot)
        log.info(
            "new bar | %s | regime=%s | adx=%.1f | atr_ratio=%.2f | spread=%s",
            last_closed_time.strftime("%H:%M"),
            regime.regime.value,
            regime.adx,
            regime.atr_ratio,
            snapshot.tick.get("spread_pips"),
        )

        # ── Run every strategy ──────────────────────────────────────────
        for strat in self.strategies:
            try:
                candidate = strat.on_tick(snapshot, regime)
            except Exception as e:
                log.exception("Strategy %s crashed: %s", strat.name, e)
                continue
            if candidate is None:
                continue
            self._handle_candidate(candidate, snapshot, regime)

    # ── Pipeline: candidate → gate → size → publish → log ──────────────

    def _handle_candidate(
        self,
        c:        Candidate,
        snapshot: MarketSnapshot,
        regime:   RegimeReading,
    ):
        log.info(
            "candidate | %s | %s | tier=%s | score=%.1f | entry=%.5f sl=%.5f tp=%.5f",
            c.strategy, c.action, c.tier, c.score,
            c.entry_price, c.sl_price, c.tp_price,
        )

        # ── Risk gate ───────────────────────────────────────────────────
        trades_today = (self.logger.get_daily_summary().get("trades") or 0)
        cons_losses  = self.logger.get_consecutive_losses()

        gate_res = self.gate.check(
            snapshot=snapshot,
            regime=regime,
            signal_action=c.action,
            sl_price=c.sl_price,
            tp_price=c.tp_price,
            entry_price=c.entry_price,
            trades_today=trades_today,
            consecutive_losses=cons_losses,
        )

        # Build signal envelope (whether sent or filtered) so the signal log
        # has identical schema for both outcomes.
        session_name = _current_session()
        sig_envelope = build_signal(
            action=c.action,
            symbol=snapshot.symbol,
            lots=0.0,          # filled below if approved
            sl_price=c.sl_price,
            tp_price=c.tp_price,
            tier=c.tier,
            strategy=c.strategy,
            regime=regime.regime.value,
            session=session_name,
            comment=f"s={c.score:.1f}",
            magic=MAGIC_NUMBER,
        )

        if not gate_res.allowed:
            log.info("gate BLOCK | %s | %s", c.strategy, gate_res.reason)
            self.logger.log_signal(sig_envelope, sent=False,
                                   filter_reason=gate_res.reason)
            return

        # ── Sizing ──────────────────────────────────────────────────────
        info = snapshot.symbol_info
        balance = float(snapshot.account.get("balance") or 0)
        lots = compute_lots(
            balance=balance,
            risk_pct=c.risk_pct,
            entry_price=c.entry_price,
            sl_price=c.sl_price,
            contract_size=float(info.get("contract_size") or 100000),
            pip_size=float(info.get("pip_size") or 0.0001),
            min_lot=float(info.get("min_lot") or 0.01),
            max_lot=float(info.get("max_lot") or 100.0),
            lot_step=float(info.get("lot_step") or 0.01),
        )
        if lots <= 0:
            log.info("gate BLOCK | %s | sizing_zero", c.strategy)
            self.logger.log_signal(sig_envelope, sent=False,
                                   filter_reason="sizing_zero")
            return

        sig_envelope["lots"] = lots

        # ── Publish + log ───────────────────────────────────────────────
        ok = self.bridge.send(sig_envelope)
        self.logger.log_signal(sig_envelope, sent=ok,
                               filter_reason="" if ok else "publish_failed")

        if ok and self.notifier.enabled:
            self.notifier.send(
                f"📤 *{c.action}* {snapshot.symbol} {lots} | tier {c.tier} | "
                f"score {c.score:.1f}\nSL {c.sl_price} | TP {c.tp_price}\n"
                f"strategy: {c.strategy} | regime: {regime.regime.value}"
            )


# ── Helpers ────────────────────────────────────────────────────────────────

def _current_session() -> str:
    """Best-fit session label for the current UTC time."""
    from config.settings import SESSIONS
    t = datetime.now(timezone.utc).time()
    for name in ("overlap", "london", "new_york"):
        cfg = SESSIONS.get(name)
        if not cfg:
            continue
        from datetime import time as dtime
        start = dtime.fromisoformat(cfg["start"])
        end   = dtime.fromisoformat(cfg["end"])
        if start <= t <= end:
            return name
    return "off"


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
