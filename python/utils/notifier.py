# python/utils/notifier.py
# ─────────────────────────────────────────────────────────────────────────────
# Telegram notifier for live alerts (signals, fills, halts, errors).
# All sends are best-effort — failures never break the trading loop.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

log = logging.getLogger("notifier")


class TelegramNotifier:
    """
    Drop-in alert channel. Reads credentials from environment so the bot
    token never lives in source control.
    """

    def __init__(
        self,
        token:   Optional[str] = None,
        chat_id: Optional[str] = None,
        timeout: float = 4.0,
    ):
        self.token   = token   or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            r = requests.post(
                url,
                data={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=self.timeout,
            )
            return r.ok
        except requests.RequestException as e:
            log.warning("Telegram send failed: %s", e)
            return False
