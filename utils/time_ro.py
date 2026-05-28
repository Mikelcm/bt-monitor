"""Romania timezone helpers — used everywhere a datetime is shown to a human.

DB stores everything UTC (proper). UI / exports show Europe/Bucharest because
that's where the operator + auditor read it. Standard format: DD.MM.YYYY HH:MM:SS.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

RO_TZ = ZoneInfo("Europe/Bucharest")
_DEFAULT_FMT = "%d.%m.%Y %H:%M:%S"
_SHORT_FMT = "%d.%m %H:%M"


def now_ro() -> datetime:
    return datetime.now(RO_TZ)


def to_ro(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(RO_TZ)


def format_ro(dt, fmt: str = _DEFAULT_FMT) -> str:
    """Format a datetime (or ISO string) in Europe/Bucharest. Empty/None → '—'."""
    if dt is None or dt == "":
        return "—"
    if isinstance(dt, str):
        try:
            # Tolerate trailing 'Z' (UTC marker)
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    local = to_ro(dt)
    if local is None:
        return "—"
    return local.strftime(fmt)


def format_ro_short(dt) -> str:
    return format_ro(dt, _SHORT_FMT)


def humanize_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = seconds / 60
        return f"{m:.1f} min"
    if seconds < 86400:
        h = seconds / 3600
        return f"{h:.1f} h"
    d = seconds / 86400
    return f"{d:.1f} zile"
