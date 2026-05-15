"""Continuous uptime watcher.

Runs an infinite loop:
    1. Probe each target URL with a fast HTTP GET (no browser).
    2. Classify the result: up / slow / down.
    3. Hysteresis — only change the official state after N consecutive
       results of the new state. This filters network blips.
    4. On state change, write a row to `state_changes` AND fire an alert.

Tunable from config or environment:
    interval_s              default 30
    slow_threshold_ms       default 3000
    hysteresis              default 3   (consecutive probes of new state)
    request_timeout_s       default 12

The watcher is designed to be embedded into the dashboard process via
`asyncio.create_task(...)`. It's also safe to run standalone:
    python -m monitoring.watcher
"""
from __future__ import annotations

import asyncio
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
from config import USER_AGENT, get_base_url
from monitoring.alerts import AlertEvent, AlertHub
from monitoring.store import UptimeStore

log = logging.getLogger("monitoring.watcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(name)s  %(message)s")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


WATCHER_INTERVAL_S      = _env_int("BT_MONITOR_WATCHER_INTERVAL_S", 30)
WATCHER_SLOW_MS         = _env_int("BT_MONITOR_WATCHER_SLOW_MS", 3000)
WATCHER_HYSTERESIS      = _env_int("BT_MONITOR_WATCHER_HYSTERESIS", 3)
WATCHER_TIMEOUT_S       = _env_int("BT_MONITOR_WATCHER_TIMEOUT_S", 12)

# Statuses we accept as "up" — anything else is "down".
ACCEPTABLE_STATUS = {200, 201, 202, 204, 301, 302, 303, 307, 308}


@dataclass
class TargetState:
    url: str
    current_state: str = "unknown"        # "up" | "slow" | "down" | "unknown"
    last_probe_state: str = "unknown"
    streak: deque = field(default_factory=lambda: deque(maxlen=WATCHER_HYSTERESIS))
    last_seen_ts: int = 0
    last_status: int | None = None
    last_response_ms: int | None = None
    last_error: str | None = None


def classify(status: int | None, response_ms: int | None, error: str | None) -> str:
    if error or status is None:
        return "down"
    if status not in ACCEPTABLE_STATUS:
        return "down"
    if response_ms is not None and response_ms > WATCHER_SLOW_MS:
        return "slow"
    return "up"


async def probe(client: httpx.AsyncClient, url: str) -> tuple[int | None, int | None, str | None]:
    """Single GET. Returns (status, response_ms, error_str)."""
    t0 = time.perf_counter()
    try:
        # Use GET (not HEAD) because Railway/Cloudflare-style stacks sometimes
        # behave differently on HEAD. We discard the body though.
        r = await client.get(url, follow_redirects=True, timeout=WATCHER_TIMEOUT_S)
        ms = int((time.perf_counter() - t0) * 1000)
        return r.status_code, ms, None
    except httpx.RequestError as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        return None, ms, f"{type(exc).__name__}: {exc}"
    except Exception as exc:
        ms = int((time.perf_counter() - t0) * 1000)
        return None, ms, repr(exc)


def _state_explanation(state: str, status: int | None, ms: int | None, err: str | None) -> str:
    if state == "down":
        if err:
            return f"network error: {err}"
        return f"HTTP {status}"
    if state == "slow":
        return f"response time {ms} ms (threshold {WATCHER_SLOW_MS} ms)"
    if state == "up":
        return f"HTTP {status} in {ms} ms"
    return state


class Watcher:
    def __init__(
        self,
        targets: Iterable[str] | None = None,
        store: UptimeStore | None = None,
        alerts: AlertHub | None = None,
    ):
        self.store = store or UptimeStore()
        self.alerts = alerts or AlertHub()
        self.targets: dict[str, TargetState] = {}
        for u in targets or []:
            self.add_target(u)
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------
    # public api
    # ------------------------------------------------------------------
    def add_target(self, url: str) -> None:
        url = url.strip().rstrip("/") or url
        if not url:
            return
        if url not in self.targets:
            self.targets[url] = TargetState(url=url)
            log.info("Watching %s", url)

    def remove_target(self, url: str) -> None:
        url = url.strip().rstrip("/") or url
        self.targets.pop(url, None)

    def set_targets(self, urls: Iterable[str]) -> None:
        wanted = {u.strip().rstrip("/") for u in urls if u and u.strip()}
        for u in list(self.targets.keys()):
            if u not in wanted:
                self.remove_target(u)
        for u in wanted:
            self.add_target(u)

    async def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    async def run_forever(self) -> None:
        log.info("Watcher starting — interval %ss, slow threshold %sms, hysteresis %s",
                 WATCHER_INTERVAL_S, WATCHER_SLOW_MS, WATCHER_HYSTERESIS)
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ro-RO,ro;q=0.9,en;q=0.8"}
        ) as client:
            self._client = client
            while not self._stop.is_set():
                if not self.targets:
                    # No targets yet — sleep a beat, maybe one gets added.
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                t0 = time.perf_counter()
                await self._tick()
                elapsed = time.perf_counter() - t0
                wait = max(0.0, WATCHER_INTERVAL_S - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        log.info("Watcher stopped.")

    async def _tick(self) -> None:
        """Probe every target in parallel, update state, write ping rows."""
        assert self._client is not None
        results = await asyncio.gather(
            *(probe(self._client, url) for url in self.targets),
            return_exceptions=False,
        )
        for url, (status, response_ms, error) in zip(self.targets, results):
            await self._handle_result(url, status, response_ms, error)

    async def _handle_result(
        self,
        url: str,
        status: int | None,
        response_ms: int | None,
        error: str | None,
    ) -> None:
        t = self.targets[url]
        probe_state = classify(status, response_ms, error)
        t.last_probe_state = probe_state
        t.last_seen_ts = int(time.time())
        t.last_status = status
        t.last_response_ms = response_ms
        t.last_error = error
        t.streak.append(probe_state)

        # Persist every ping (downsampled later via 24h cutoff in store).
        self.store.record_ping(url, probe_state, status, response_ms, error)

        # Hysteresis — change the OFFICIAL state only after N matching probes.
        n_needed = max(1, WATCHER_HYSTERESIS)
        confirmed = (len(t.streak) >= n_needed and len(set(list(t.streak)[-n_needed:])) == 1
                     and t.streak[-1] != t.current_state)
        # First-ever state is set immediately on the first probe.
        first_state = t.current_state == "unknown" and probe_state != "unknown"
        if confirmed or first_state:
            old = t.current_state
            new = probe_state
            t.current_state = new
            msg = _state_explanation(new, status, response_ms, error)
            self.store.record_state_change(url, old, new, msg, status, response_ms)
            await self.alerts.fire(AlertEvent(
                ts=t.last_seen_ts, url=url,
                from_state=old, to_state=new,
                message=msg, status=status, response_ms=response_ms,
            ))
            log.info("STATE %s : %s -> %s (%s)", url, old, new, msg)


# ---------------------------------------------------------------------
# standalone entry point — `python -m monitoring.watcher`
# ---------------------------------------------------------------------
async def _main() -> None:
    targets = [get_base_url()]
    w = Watcher(targets=targets)
    try:
        await w.run_forever()
    except KeyboardInterrupt:
        await w.stop()


if __name__ == "__main__":
    asyncio.run(_main())
