"""SQLite persistence for the live watcher.

Two tables:
    pings           — every individual probe (one row per probe per URL)
    state_changes   — only the transitions (up → down, down → up, etc.)

We keep `pings` rolling — older than 24h is auto-pruned on insert so the
db stays small. `state_changes` keep forever (it's the incident log).

Why sqlite and not just a JSON file: we need INDEX on timestamp to query
the last N pings quickly, and we want atomic writes from concurrent reads
(the watcher writes while the dashboard reads).
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH_DEFAULT = Path(__file__).resolve().parent.parent / "data" / "uptime.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    url         TEXT    NOT NULL,
    status      INTEGER,
    response_ms INTEGER,
    state       TEXT    NOT NULL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_pings_url_ts ON pings (url, ts DESC);

CREATE TABLE IF NOT EXISTS state_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    url         TEXT    NOT NULL,
    from_state  TEXT    NOT NULL,
    to_state    TEXT    NOT NULL,
    message     TEXT,
    response_ms INTEGER,
    status      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_changes_ts ON state_changes (ts DESC);
"""


class UptimeStore:
    """Thin sqlite3 wrapper. Sync, but operations are tiny (< 1 ms)."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH_DEFAULT
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.db_path, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def record_ping(
        self,
        url: str,
        state: str,
        status: int | None,
        response_ms: int | None,
        error: str | None,
    ) -> None:
        ts = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO pings (ts, url, status, response_ms, state, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, url, status, response_ms, state, error),
            )
            # Keep only the last 24h of pings — keeps the DB tiny.
            cutoff = ts - 24 * 3600
            c.execute("DELETE FROM pings WHERE ts < ?", (cutoff,))

    def record_state_change(
        self,
        url: str,
        from_state: str,
        to_state: str,
        message: str = "",
        status: int | None = None,
        response_ms: int | None = None,
    ) -> int:
        ts = int(time.time())
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO state_changes (ts, url, from_state, to_state, message, response_ms, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, url, from_state, to_state, message, response_ms, status),
            )
            return int(cur.lastrowid or 0)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def recent_pings(self, url: str, limit: int = 120) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, status, response_ms, state, error FROM pings "
                "WHERE url = ? ORDER BY ts DESC LIMIT ?",
                (url, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]   # oldest → newest

    def uptime_percent(self, url: str, window_seconds: int = 24 * 3600) -> float | None:
        cutoff = int(time.time()) - window_seconds
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM pings WHERE url = ? AND ts >= ?",
                (url, cutoff),
            ).fetchone()[0]
            if total == 0:
                return None
            up = c.execute(
                "SELECT COUNT(*) FROM pings WHERE url = ? AND ts >= ? AND state IN ('up','slow')",
                (url, cutoff),
            ).fetchone()[0]
        return round(up / total * 100, 2)

    def latest_state(self, url: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT ts, status, response_ms, state, error FROM pings "
                "WHERE url = ? ORDER BY ts DESC LIMIT 1",
                (url,),
            ).fetchone()
        return dict(row) if row else None

    def recent_state_changes(self, limit: int = 25) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, url, from_state, to_state, message, response_ms, status "
                "FROM state_changes ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def known_urls(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT url, MAX(ts) AS last_ts FROM pings GROUP BY url ORDER BY last_ts DESC"
            ).fetchall()
        return [r["url"] for r in rows]
