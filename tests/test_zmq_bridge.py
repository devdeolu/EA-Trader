# tests/test_zmq_bridge.py
# ─────────────────────────────────────────────────────────────────────────────
# Loopback test for the ZMQ bridge — no MT5 needed.
# ─────────────────────────────────────────────────────────────────────────────

import json
import time

import pytest
import zmq

from python.core.zmq_bridge import build_signal


def test_build_signal_defaults():
    sig = build_signal("BUY", lots=0.02, sl_price=1.0800, tp_price=1.0900)
    assert sig["action"]   == "BUY"
    assert sig["lots"]     == 0.02
    assert sig["magic"]    == 20260101
    assert "ts" in sig


def test_pub_sub_loopback():
    ctx = zmq.Context()
    pub = ctx.socket(zmq.PUB)
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt_string(zmq.SUBSCRIBE, "")
    sub.setsockopt(zmq.RCVTIMEO, 1000)
    try:
        pub.bind("tcp://127.0.0.1:5598")
        sub.connect("tcp://127.0.0.1:5598")
        time.sleep(0.3)

        sig = build_signal("SELL", lots=0.01, sl_price=1.0950, tp_price=1.0850,
                           tier="A", strategy="trend_pullback", regime="trending")
        pub.send_string(json.dumps(sig))

        raw = sub.recv_string()
        data = json.loads(raw)
        assert data["action"]   == "SELL"
        assert data["strategy"] == "trend_pullback"
    finally:
        pub.close()
        sub.close()
        ctx.term()
