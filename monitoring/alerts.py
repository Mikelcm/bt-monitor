"""Alert dispatchers — fan out a state-change event to one or more channels.

For the local demo we currently dispatch to:
  * the dashboard's in-memory event log (shown in the UI banner)
  * (optional) a Microsoft Teams webhook if BT_MONITOR_TEAMS_WEBHOOK is set

The Teams path is best-effort — failures are logged and do NOT
interrupt the watcher loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

import httpx

log = logging.getLogger("monitoring.alerts")


@dataclass
class AlertEvent:
    ts: int
    url: str
    from_state: str
    to_state: str
    message: str
    status: int | None = None
    response_ms: int | None = None

    @property
    def severity(self) -> str:
        if self.to_state == "down":
            return "critical"
        if self.to_state == "slow":
            return "warn"
        if self.to_state == "up" and self.from_state in ("down", "slow"):
            return "recovery"
        return "info"

    def human(self) -> str:
        when = datetime.fromtimestamp(self.ts, tz=timezone.utc).strftime("%H:%M:%S UTC")
        return f"[{when}] {self.url} : {self.from_state} → {self.to_state} · {self.message}"


class AlertHub:
    """In-memory ring buffer + outbound webhook dispatch."""

    def __init__(self, ring_size: int = 50):
        self._events: list[AlertEvent] = []
        self._ring_size = ring_size
        self._lock = asyncio.Lock()

    async def fire(self, event: AlertEvent) -> None:
        async with self._lock:
            self._events.append(event)
            if len(self._events) > self._ring_size:
                self._events = self._events[-self._ring_size:]
        log.warning("ALERT %s", event.human())
        # Fan out to webhooks (best-effort, never raises).
        try:
            await self._send_teams(event)
        except Exception as exc:
            log.warning("Teams webhook failed: %r", exc)

    def recent(self) -> list[AlertEvent]:
        return list(reversed(self._events))   # newest first

    # ------------------------------------------------------------------
    # outbound webhooks
    # ------------------------------------------------------------------
    async def _send_teams(self, event: AlertEvent) -> None:
        url = os.environ.get("BT_MONITOR_TEAMS_WEBHOOK")
        if not url:
            return
        color = {"critical": "ef4444", "warn": "f59e0b",
                 "recovery": "10b981", "info": "64748b"}[event.severity]
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": color,
            "title": f"BT Monitor: {event.url}",
            "text": event.message,
            "sections": [{
                "facts": [
                    {"name": "From", "value": event.from_state},
                    {"name": "To",   "value": event.to_state},
                    {"name": "HTTP", "value": str(event.status) if event.status else "—"},
                    {"name": "Response", "value": f"{event.response_ms} ms" if event.response_ms else "—"},
                ]
            }],
        }
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(url, json=card)
        except Exception as exc:
            log.warning("Teams webhook failed: %r", exc)

