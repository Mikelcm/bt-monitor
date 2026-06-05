"""Performance regression detection.

A fixed "slow if > 3s" threshold misses the most common real-world degradation:
a page that used to load in 600ms and now takes 1.4s — still under the hard
threshold, but 2.3x slower. Users feel it; a flat threshold doesn't catch it.

This module baselines each URL against its own recent history (the latency
samples the live watchers already persist in `uptime_checks`) and flags a
**regression** when the recent average is materially slower than the 7-day
baseline. It opens/closes incidents of category `perf_regression` so they flow
through the same dashboard + alerting pipeline as everything else.

Only `state='up'` probes are averaged, so genuine outages/slow-spikes (already
covered by site_down/page_slow) don't pollute the baseline, and the detector
focuses on sub-threshold creep.

Config (env, all optional):
  BT_MONITOR_PERF_FACTOR              default 1.5   (recent >= baseline * factor)
  BT_MONITOR_PERF_BASELINE_DAYS       default 7
  BT_MONITOR_PERF_EXCLUDE_RECENT_H    default 2     (keep the current window out of the baseline)
  BT_MONITOR_PERF_RECENT_H           default 1
  BT_MONITOR_PERF_MIN_BASELINE        default 30    (min baseline samples)
  BT_MONITOR_PERF_MIN_RECENT          default 5     (min recent samples)
  BT_MONITOR_PERF_MIN_MS              default 150   (ignore very fast pages)
  BT_MONITOR_PERF_CHECK_INTERVAL_MIN  default 15
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import func as sa_func, select

from db.models import Incident, UptimeCheck, get_session, init_db
from monitoring.incident_alerts import IncidentAlert, hub as alert_hub

log = logging.getLogger("monitoring.perf_regression")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


FACTOR = _f("BT_MONITOR_PERF_FACTOR", 1.5)
BASELINE_DAYS = _i("BT_MONITOR_PERF_BASELINE_DAYS", 7)
EXCLUDE_RECENT_H = _f("BT_MONITOR_PERF_EXCLUDE_RECENT_H", 2.0)
RECENT_H = _f("BT_MONITOR_PERF_RECENT_H", 1.0)
MIN_BASELINE = _i("BT_MONITOR_PERF_MIN_BASELINE", 30)
MIN_RECENT = _i("BT_MONITOR_PERF_MIN_RECENT", 5)
MIN_MS = _f("BT_MONITOR_PERF_MIN_MS", 150)
CHECK_INTERVAL_MIN = _f("BT_MONITOR_PERF_CHECK_INTERVAL_MIN", 15)

_CATEGORY = "perf_regression"
_SEVERITY = "moderate"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(target: str) -> str:
    return f"{_CATEGORY}|{target}"


def _avg_latency(session, target: str, since: datetime, until: datetime | None = None) -> tuple[float | None, int]:
    """Average response_ms over 'up' probes in the window. Returns (avg, count)."""
    conds = [
        UptimeCheck.target_url == target,
        UptimeCheck.state == "up",
        UptimeCheck.response_ms.is_not(None),
        UptimeCheck.checked_at >= since,
    ]
    if until is not None:
        conds.append(UptimeCheck.checked_at < until)
    row = session.execute(
        select(sa_func.avg(UptimeCheck.response_ms), sa_func.count(UptimeCheck.id)).where(*conds)
    ).first()
    avg = float(row[0]) if row and row[0] is not None else None
    cnt = int(row[1]) if row and row[1] is not None else 0
    return avg, cnt


def evaluate(session, target: str, now: datetime | None = None) -> dict:
    """Compute the regression verdict for one target. Pure read; no writes.

    Returns {target, baseline_ms, recent_ms, pct, samples_baseline, samples_recent,
             regressed, enough_data}.
    """
    now = now or _utcnow()
    baseline_start = now - timedelta(days=BASELINE_DAYS)
    baseline_end = now - timedelta(hours=EXCLUDE_RECENT_H)
    recent_start = now - timedelta(hours=RECENT_H)

    base_ms, base_n = _avg_latency(session, target, baseline_start, baseline_end)
    rec_ms, rec_n = _avg_latency(session, target, recent_start)

    enough = (
        base_ms is not None and rec_ms is not None
        and base_n >= MIN_BASELINE and rec_n >= MIN_RECENT
        and base_ms >= MIN_MS
    )
    pct = None
    regressed = False
    if enough:
        pct = round((rec_ms - base_ms) / base_ms * 100, 1)
        regressed = rec_ms >= base_ms * FACTOR
    return {
        "target": target,
        "baseline_ms": round(base_ms) if base_ms is not None else None,
        "recent_ms": round(rec_ms) if rec_ms is not None else None,
        "pct": pct,
        "samples_baseline": base_n,
        "samples_recent": rec_n,
        "regressed": regressed,
        "enough_data": enough,
    }


def perf_snapshot(target: str) -> dict:
    """UI helper: latency baseline vs recent for one target (read-only)."""
    init_db()
    with get_session() as session:
        return evaluate(session, target.rstrip("/"))


def _targets_with_recent_data(session, now: datetime) -> list[str]:
    since = now - timedelta(hours=RECENT_H)
    rows = session.execute(
        select(UptimeCheck.target_url)
        .where(UptimeCheck.checked_at >= since)
        .distinct()
    ).all()
    return [r[0] for r in rows]


def check_perf_regressions() -> dict:
    """Evaluate every active target; open/resolve perf_regression incidents.

    Returns stats {opened, resolved, checked}.
    """
    init_db()
    now = _utcnow()
    stats = {"opened": 0, "resolved": 0, "checked": 0}
    events: list[IncidentAlert] = []

    with get_session() as session:
        targets = _targets_with_recent_data(session, now)
        for target in targets:
            verdict = evaluate(session, target, now)
            if not verdict["enough_data"]:
                continue
            stats["checked"] += 1
            fp = _fingerprint(target)
            existing = session.scalar(
                select(Incident).where(Incident.target_url == target, Incident.fingerprint == fp)
            )
            if verdict["regressed"]:
                summary = (
                    f"Regresie de performanță: {target} — {verdict['recent_ms']} ms "
                    f"acum vs {verdict['baseline_ms']} ms baseline ({verdict['pct']:+}%)"
                )
                details = {k: verdict[k] for k in ("baseline_ms", "recent_ms", "pct", "samples_baseline", "samples_recent")}
                if existing is None:
                    inc = Incident(
                        target_url=target, fingerprint=fp, category=_CATEGORY, severity=_SEVERITY,
                        page_url=target, summary=summary[:1024], details=details,
                        first_seen_at=now, last_seen_at=now,
                    )
                    session.add(inc)
                    session.flush()
                    stats["opened"] += 1
                    events.append(_event("new", inc))
                else:
                    existing.last_seen_at = now
                    existing.summary = summary[:1024]
                    existing.details = details
                    if existing.resolved_at is not None:
                        existing.resolved_at = None
                        stats["opened"] += 1
                        events.append(_event("reopened", existing))
            else:
                # recovered — resolve an open regression incident if present
                if existing is not None and existing.resolved_at is None:
                    existing.resolved_at = now
                    stats["resolved"] += 1
                    events.append(_event("resolved", existing))
        session.commit()

    if events:
        try:
            alert_hub.fire_batch(events)
        except Exception as exc:
            log.warning("perf alert dispatch failed: %r", exc)
    return stats


def _event(kind: str, inc: Incident) -> IncidentAlert:
    return IncidentAlert(
        kind=kind, incident_id=inc.id, fingerprint=inc.fingerprint,
        category=inc.category, severity=inc.severity, summary=inc.summary,
        target_url=inc.target_url, page_url=inc.page_url, run_id=0,
    )
