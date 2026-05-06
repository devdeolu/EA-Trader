# python/core/zmq_bridge.py
# ─────────────────────────────────────────────────────────────────────────────
# ZeroMQ signal bus between Python brain and MT5 execution wrapper.
#
# Architecture:
#   Python (PUB) ──── signals ────► MQL5 (SUB)   port 5555
#   MQL5   (PUB) ──── results ────► Python (SUB)  port 5556
#
# Signal format (Python → MT5):  JSON string
# Result format (MT5 → Python):  JSON string
#
# All messages are fire-and-forget PUB/SUB — no blocking waits.
# The MQL5 guard layer handles safety independently of this pipe.
# ─────────────────────────────────────────────────────────────────────────────

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import zmq

from config.settings import LOG_LEVEL, ZMQ_DATA_PORT, ZMQ_HOST, ZMQ_SIGNAL_PORT
from python.utils.logger import TradeLogger

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("zmq_bridge")


# ── Signal schema ─────────────────────────────────────────────────────────────

def build_signal(
    action:     str,            # "BUY" | "SELL" | "CLOSE_ALL" | "CLOSE" | "HEARTBEAT"
    symbol:     str   = "EURUSD",
    lots:       float = 0.01,
    sl_price:   float = 0.0,    # absolute price (0 = no SL)
    tp_price:   float = 0.0,    # absolute price (0 = no TP)
    ticket:     int   = 0,      # required for CLOSE
    tier:       str   = "B",    # "A" | "B"
    strategy:   str   = "",     # e.g. "trend_pullback"
    regime:     str   = "",     # e.g. "trending" | "ranging"
    session:    str   = "",     # e.g. "london" | "new_york"
    comment:    str   = "",
    magic:      int   = 20260101,
) -> dict:
    """
    Build a validated signal dict. Every field has a safe default.
    The MQL5 receiver ignores fields it doesn't recognise — forward compatible.
    """
    return {
        "action":    action.upper(),
        "symbol":    symbol,
        "lots":      round(lots, 2),
        "sl_price":  round(sl_price, 5),
        "tp_price":  round(tp_price, 5),
        "ticket":    ticket,
        "tier":      tier,
        "strategy":  strategy,
        "regime":    regime,
        "session":   session,
        "comment":   comment[:32],          # MT5 comment field max 32 chars
        "magic":     magic,
        "ts":        datetime.now(timezone.utc).isoformat(),
    }


# ── ZMQ publisher (Python → MT5) ─────────────────────────────────────────────

class SignalPublisher:
    """
    Publishes trade signals to the MQL5 receiver via ZeroMQ PUB socket.
    Thread-safe — multiple strategy threads can call publish() concurrently.
    """

    def __init__(
        self,
        host: str = ZMQ_HOST,
        port: int = ZMQ_SIGNAL_PORT,
    ):
        self._ctx    = zmq.Context()
        self._socket = self._ctx.socket(zmq.PUB)
        self._socket.setsockopt(zmq.SNDHWM, 1000)
        self._addr   = f"tcp://{host}:{port}"
        self._lock   = threading.Lock()
        self._connected = False

    def connect(self):
        self._socket.bind(self._addr)
        # PUB sockets need a brief warm-up before subscribers connect
        time.sleep(0.5)
        self._connected = True
        log.info(f"Signal publisher bound to {self._addr}")

    def publish(self, signal: dict) -> bool:
        """
        Serialize and send a signal. Returns True on success.
        """
        if not self._connected:
            log.error("Publisher not connected. Call connect() first.")
            return False

        try:
            payload = json.dumps(signal)
            with self._lock:
                self._socket.send_string(payload, zmq.NOBLOCK)
            log.info(f"SIGNAL SENT | {signal['action']:10s} | "
                     f"{signal['symbol']} | lots={signal['lots']} | "
                     f"SL={signal['sl_price']} | TP={signal['tp_price']} | "
                     f"tier={signal['tier']} | strategy={signal['strategy']}")
            return True
        except zmq.ZMQError as e:
            log.error(f"ZMQ send error: {e}")
            return False

    def send_heartbeat(self):
        """Send a heartbeat every N seconds so MT5 knows Python is alive."""
        hb = build_signal("HEARTBEAT")
        self.publish(hb)

    def close(self):
        self._socket.close()
        self._ctx.term()
        self._connected = False
        log.info("Signal publisher closed.")


# ── ZMQ subscriber (MT5 → Python) ────────────────────────────────────────────

class ResultSubscriber:
    """
    Subscribes to trade result messages published by the MQL5 receiver.
    Runs in a background thread and passes each result to a callback.

    Result payload from MQL5 (example):
    {
        "event":      "TRADE_CLOSED",
        "ticket":     12345678,
        "symbol":     "EURUSD",
        "type":       "BUY",
        "volume":     0.02,
        "open_price": 1.08450,
        "close_price":1.08620,
        "profit":     34.00,
        "commission": -0.70,
        "swap":       0.00,
        "net_profit": 33.30,
        "open_time":  "2026-01-15T09:32:00+00:00",
        "close_time": "2026-01-15T11:14:00+00:00",
        "magic":      20260101,
        "comment":    "apex_tp1"
    }
    """

    def __init__(
        self,
        host:     str      = ZMQ_HOST,
        port:     int      = ZMQ_DATA_PORT,
        callback: Optional[Callable[[dict], None]] = None,
        logger:   Optional["TradeLogger"] = None,
    ):
        self._ctx      = zmq.Context()
        self._socket   = self._ctx.socket(zmq.SUB)
        self._socket.setsockopt_string(zmq.SUBSCRIBE, "")   # receive all
        self._socket.setsockopt(zmq.RCVTIMEO, 1000)         # 1s timeout
        self._addr     = f"tcp://{host}:{port}"
        self._callback = callback or self._default_callback
        self._logger   = logger
        self._running  = False
        self._thread   = None

    def connect(self):
        self._socket.connect(self._addr)
        log.info(f"Result subscriber connected to {self._addr}")

    def start(self):
        """Start background listener thread."""
        self._running = True
        self._thread  = threading.Thread(
            target=self._listen, daemon=True, name="zmq_result_listener"
        )
        self._thread.start()
        log.info("Result listener started.")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self._socket.close()
        self._ctx.term()
        log.info("Result listener stopped.")

    def _listen(self):
        while self._running:
            try:
                raw = self._socket.recv_string()
                data = json.loads(raw)
                self._callback(data)
                if self._logger and data.get("event") == "TRADE_CLOSED":
                    self._logger.log_trade(data)
            except zmq.Again:
                pass    # timeout — normal, just loop
            except json.JSONDecodeError as e:
                log.warning(f"Bad JSON from MT5: {e} | raw={raw[:80]}")
            except Exception as e:
                log.error(f"Result listener error: {e}")

    @staticmethod
    def _default_callback(data: dict):
        event = data.get("event", "UNKNOWN")
        log.info(f"MT5 EVENT | {event} | {json.dumps(data)}")


# ── Heartbeat thread ──────────────────────────────────────────────────────────

class HeartbeatThread(threading.Thread):
    """
    Sends a heartbeat signal every `interval` seconds.
    MT5 receiver can use this to detect Python crashes.
    """
    def __init__(self, publisher: SignalPublisher, interval: int = 10):
        super().__init__(daemon=True, name="heartbeat")
        self.publisher = publisher
        self.interval  = interval
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.wait(self.interval):
            self.publisher.send_heartbeat()

    def stop(self):
        self._stop_evt.set()


# ── Bridge orchestrator ───────────────────────────────────────────────────────

class ZMQBridge:
    """
    Top-level object that owns the publisher, subscriber, and heartbeat.
    Typical usage:

        bridge = ZMQBridge()
        bridge.start()

        signal = build_signal("BUY", lots=0.02, sl_price=1.0800, tp_price=1.0900)
        bridge.send(signal)

        bridge.stop()
    """

    def __init__(
        self,
        on_result: Optional[Callable[[dict], None]] = None,
        logger:    Optional["TradeLogger"] = None,
    ):
        self.publisher  = SignalPublisher()
        self.subscriber = ResultSubscriber(
            callback=on_result,
            logger=logger,
        )
        self.heartbeat  = HeartbeatThread(self.publisher)

    def start(self):
        self.publisher.connect()
        self.subscriber.connect()
        self.subscriber.start()
        self.heartbeat.start()
        log.info("ZMQ bridge fully started.")

    def send(self, signal: dict) -> bool:
        return self.publisher.publish(signal)

    def stop(self):
        self.heartbeat.stop()
        self.subscriber.stop()
        self.publisher.close()
        log.info("ZMQ bridge stopped.")


# ── Quick test (no MT5 needed — just tests the socket layer) ─────────────────
if __name__ == "__main__":
    import sys

    print("Testing ZMQ bridge (loopback — no MT5 required)...")

    received = []

    def on_msg(data):
        received.append(data)
        print(f"  Received: {data}")

    # Loopback test: subscriber connects to publisher's own port
    ctx   = zmq.Context()
    pub   = ctx.socket(zmq.PUB)
    sub   = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.setsockopt(zmq.RCVTIMEO, 500)

    pub.bind("tcp://127.0.0.1:5599")
    sub.connect("tcp://127.0.0.1:5599")
    time.sleep(0.3)

    test_signals = [
        build_signal("BUY",  lots=0.02, sl_price=1.0800, tp_price=1.0900,
                     tier="A", strategy="trend_pullback", regime="trending"),
        build_signal("SELL", lots=0.01, sl_price=1.0950, tp_price=1.0850,
                     tier="B", strategy="mean_reversion", regime="ranging"),
        build_signal("HEARTBEAT"),
    ]

    for sig in test_signals:
        payload = json.dumps(sig)
        pub.send_string(payload, zmq.NOBLOCK)
        time.sleep(0.05)

    for _ in test_signals:
        try:
            raw  = sub.recv_string()
            data = json.loads(raw)
            print(f"  OK: {data['action']:12s} | lots={data.get('lots')} | "
                  f"strategy={data.get('strategy')}")
        except zmq.Again:
            print("  TIMEOUT — message not received")

    pub.close()
    sub.close()
    ctx.term()
    print(f"\nZMQ bridge test: {len(test_signals)} sent, "
          f"{len(test_signals)} received — OK")
