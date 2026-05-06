# python/utils/news_guard.py
# ─────────────────────────────────────────────────────────────────────────────
# Economic calendar gate.
#
# Pulls high-impact events for the symbol's currencies, caches once per day,
# and exposes is_blackout(now) so the risk gate can suppress entries within
# NEWS_BLACKOUT_MINUTES of any event.
#
# Provider: ForexFactory weekly XML (free, no key). Easy to swap to Finnhub
# or another provider — the public API (load(), is_blackout(), next_event())
# stays the same.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config.settings import DATA_DIR, LOG_LEVEL, NEWS_BLACKOUT_MINUTES, SYMBOL

logging.basicConfig(level=getattr(logging, LOG_LEVEL))
log = logging.getLogger("news_guard")

FF_XML_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
CACHE_FILE = os.path.join(DATA_DIR, "ff_calendar.xml")


# ── Currency map for symbols ────────────────────────────────────────────────
# Extend as needed when more symbols are added.
SYMBOL_CCYS: dict[str, tuple[str, ...]] = {
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "XAUUSD": ("USD",),
}


@dataclass(frozen=True)
class NewsEvent:
    when:     datetime          # UTC
    currency: str
    title:    str
    impact:   str               # "High" | "Medium" | "Low"


class NewsGuard:
    """
    Lightweight, file-cached economic calendar guard.

    Usage:
        guard = NewsGuard()
        guard.load()                          # call once at startup, refresh daily
        if guard.is_blackout(datetime.now(timezone.utc)):
            ...                               # skip trading
    """

    def __init__(
        self,
        symbol:           str = SYMBOL,
        blackout_minutes: int = NEWS_BLACKOUT_MINUTES,
        impact_filter:    tuple[str, ...] = ("High",),
    ):
        self.symbol           = symbol
        self.currencies       = SYMBOL_CCYS.get(symbol, ())
        self.blackout_minutes = blackout_minutes
        self.impact_filter    = impact_filter
        self.events: list[NewsEvent] = []
        self._loaded_on:  Optional[datetime] = None

    # ── Loading ──────────────────────────────────────────────────────────

    def load(self, force: bool = False) -> int:
        """Fetch + parse the calendar. Returns count of relevant events."""
        os.makedirs(DATA_DIR, exist_ok=True)
        today = datetime.now(timezone.utc).date()

        need_download = (
            force
            or not os.path.exists(CACHE_FILE)
            or datetime.fromtimestamp(
                os.path.getmtime(CACHE_FILE), tz=timezone.utc
            ).date() < today
        )

        if need_download:
            try:
                resp = requests.get(FF_XML_URL, timeout=15)
                resp.raise_for_status()
                with open(CACHE_FILE, "wb") as f:
                    f.write(resp.content)
                log.info("News calendar refreshed (%d bytes)", len(resp.content))
            except requests.RequestException as e:
                log.warning("News fetch failed (%s) — using cache if present", e)
                if not os.path.exists(CACHE_FILE):
                    return 0

        self.events     = self._parse(CACHE_FILE)
        self._loaded_on = datetime.now(timezone.utc)
        log.info("Loaded %d %s events for %s",
                 len(self.events), self.impact_filter, self.symbol)
        return len(self.events)

    def _parse(self, path: str) -> list[NewsEvent]:
        try:
            tree = ET.parse(path)
        except ET.ParseError as e:
            log.error("Calendar XML parse failed: %s", e)
            return []

        out: list[NewsEvent] = []
        for ev in tree.getroot().findall("event"):
            currency = (ev.findtext("country") or "").strip().upper()
            impact   = (ev.findtext("impact")  or "").strip()
            title    = (ev.findtext("title")   or "").strip()
            date_s   = ev.findtext("date")
            time_s   = ev.findtext("time")

            if currency not in self.currencies:
                continue
            if impact not in self.impact_filter:
                continue
            if not date_s or not time_s:
                continue

            # FF format: date=MM-DD-YYYY, time=HH:MMam/pm (US Eastern)
            try:
                dt_local = datetime.strptime(
                    f"{date_s} {time_s}", "%m-%d-%Y %I:%M%p"
                )
                # Treat as US/Eastern; convert to UTC. Without zoneinfo the
                # rough offset is fine for blackout windows (±30 min default).
                dt_utc = dt_local.replace(tzinfo=timezone.utc) + timedelta(hours=5)
            except ValueError:
                continue

            out.append(NewsEvent(
                when=dt_utc, currency=currency, title=title, impact=impact
            ))

        out.sort(key=lambda e: e.when)
        return out

    # ── Query API ────────────────────────────────────────────────────────

    def is_blackout(self, now: Optional[datetime] = None) -> bool:
        now = now or datetime.now(timezone.utc)
        window = timedelta(minutes=self.blackout_minutes)
        return any(abs(ev.when - now) <= window for ev in self.events)

    def next_event(self, now: Optional[datetime] = None) -> Optional[NewsEvent]:
        now = now or datetime.now(timezone.utc)
        future = [ev for ev in self.events if ev.when >= now]
        return future[0] if future else None
