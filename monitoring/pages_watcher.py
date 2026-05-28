"""Per-page uptime watcher — probes every URL from data/sitemap_pages.json.

Complements the main Watcher (which monitors the root URL + user-added critical
pages at ~30s). This one runs slower (default 2 min) so that 100+ sitemap pages
don't get flooded with probes.

Creates incidents of:
  category=page_down  severity=serious   (when a sitemap page goes down)
  category=page_slow  severity=moderate  (when it goes slow)

So in dashboards / exports the operator can distinguish:
  site_down  → root is down, entire site affected     (CRITICAL)
  page_down  → one page is broken, site still mostly OK (SERIOUS)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import USER_AGENT
from monitoring.uptime_persist import handle_state_change, record_probe
from monitoring.watcher import classify, probe

log = logging.getLogger("monitoring.pages_watcher")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


PAGES_INTERVAL_S = _env_int("BT_MONITOR_PAGES_INTERVAL_S", 120)
PAGES_HYSTERESIS = _env_int("BT_MONITOR_PAGES_HYSTERESIS", 2)
PAGES_CONCURRENCY = _env_int("BT_MONITOR_PAGES_CONCURRENCY", 10)

SITEMAP_FILE = ROOT / "data" / "sitemap_pages.json"


@dataclass
class PageState:
    url: str
    current_state: str = "unknown"
    streak: deque = field(default_factory=lambda: deque(maxlen=PAGES_HYSTERESIS))
    last_seen_ts: int = 0
    last_status: int | None = None
    last_response_ms: int | None = None
    last_error: str | None = None


def _load_sitemap_urls() -> list[str]:
    if not SITEMAP_FILE.exists():
        return []
    try:
        data = json.loads(SITEMAP_FILE.read_text(encoding="utf-8"))
        urls = data.get("page_urls") or []
        return [u.rstrip("/") for u in urls if u]
    except Exception:
        return []


class PagesWatcher:
    """Periodically probes every URL in sitemap_pages.json.

    Re-reads the sitemap file each tick so new pages from the latest deep scan
    are picked up automatically. URLs removed from the sitemap have their state
    discarded.
    """

    def __init__(self):
        self.states: dict[str, PageState] = {}
        self._stop = asyncio.Event()
        self._sem = asyncio.Semaphore(PAGES_CONCURRENCY)

    async def stop(self) -> None:
        self._stop.set()

    @property
    def page_count(self) -> int:
        return len(self.states)

    @property
    def states_summary(self) -> dict[str, int]:
        """Counts of pages per current state."""
        out: dict[str, int] = {}
        for s in self.states.values():
            out[s.current_state] = out.get(s.current_state, 0) + 1
        return out

    async def run_forever(self) -> None:
        log.info("PagesWatcher starting — interval %ss, hysteresis %s, concurrency %s",
                 PAGES_INTERVAL_S, PAGES_HYSTERESIS, PAGES_CONCURRENCY)
        # short grace period: let main Watcher boot + first scan possibly complete
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
            return
        except asyncio.TimeoutError:
            pass

        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"}
        ) as client:
            while not self._stop.is_set():
                t0 = time.perf_counter()
                try:
                    await self._tick(client)
                except Exception as exc:
                    log.warning("PagesWatcher tick failed: %r", exc)
                elapsed = time.perf_counter() - t0
                wait = max(5.0, PAGES_INTERVAL_S - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        log.info("PagesWatcher stopped.")

    async def _tick(self, client: httpx.AsyncClient) -> None:
        urls = _load_sitemap_urls()
        if not urls:
            return

        # Add new URLs, drop removed ones.
        wanted = set(urls)
        for url in list(self.states.keys()):
            if url not in wanted:
                self.states.pop(url, None)
        for url in urls:
            if url not in self.states:
                self.states[url] = PageState(url=url)

        # Probe in parallel (bounded).
        async def _probe_one(url: str):
            async with self._sem:
                return url, await probe(client, url)

        results = await asyncio.gather(*[_probe_one(u) for u in urls], return_exceptions=False)

        for url, (status, response_ms, error) in results:
            s = self.states[url]
            probe_state = classify(status, response_ms, error)
            s.last_seen_ts = int(time.time())
            s.last_status = status
            s.last_response_ms = response_ms
            s.last_error = error
            s.streak.append(probe_state)

            # Persist every probe.
            record_probe(url, probe_state, status, response_ms, error)

            # Hysteresis: confirmed only after N consecutive identical probes.
            n_needed = max(1, PAGES_HYSTERESIS)
            confirmed = (
                len(s.streak) >= n_needed
                and len(set(list(s.streak)[-n_needed:])) == 1
                and s.streak[-1] != s.current_state
            )
            first_state = s.current_state == "unknown" and probe_state != "unknown"
            if confirmed or first_state:
                old = s.current_state
                new = probe_state
                s.current_state = new
                msg = f"Page transition {old} → {new}"
                try:
                    handle_state_change(
                        url, old, new, status, response_ms, error, msg,
                        kind_for_down="page_down",
                        kind_for_slow="page_slow",
                        severity_down="serious",
                        severity_slow="moderate",
                    )
                except Exception as exc:
                    log.warning("page incident hook failed for %s: %r", url, exc)
                log.info("PAGE %s : %s -> %s (%s)", url, old, new, msg)


# Standalone entry point — useful for manual testing.
async def _main() -> None:
    w = PagesWatcher()
    try:
        await w.run_forever()
    except KeyboardInterrupt:
        await w.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(name)s  %(message)s")
    asyncio.run(_main())
