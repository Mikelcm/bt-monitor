"""Bridge between live watcher and DB.

Two responsibilities:
  1. `record_probe()` — append every probe outcome to `uptime_checks`.
  2. `handle_state_change()` — when a target transitions up/slow/down,
     open / close an `Incident` of category `site_down` or `page_slow`
     so it shows up in the same incident pipeline (dashboard + alerts)
     as deep-scan findings.

Also exposes `uptime_percent()` and `prune_old()` for the UI / scheduler.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, select, func as sa_func

from db.models import Incident, IncidentObservation, Run, UptimeCheck, get_session, init_db
from monitoring.incident_alerts import IncidentAlert, hub as alert_hub

log = logging.getLogger("monitoring.uptime_persist")

# #9 — observability for persistence failures. The dashboard /settings page and
# tests can read this to surface "probes lost to DB errors" instead of failing
# silently. init_db() is NOT called per-probe anymore (#10): the dashboard and
# runner call it once at startup; init_db() is itself an idempotent no-op after
# the first call as a safety net.
_persist_failures = 0


def persist_failure_count() -> int:
    return _persist_failures


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(target: str, kind: str) -> str:
    """One incident per (target, kind). Re-opens if the same target goes
    down twice. Kind ∈ {site_down, site_slow, page_down, page_slow}."""
    return f"uptime_{kind}|{target}"


_KIND_LABELS_RO = {
    "site_down": "Site indisponibil",
    "site_slow": "Site lent",
    "page_down": "Pagină indisponibilă",
    "page_slow": "Pagină lentă",
}


def _summary(target: str, kind: str, status_code: int | None, error: str | None) -> str:
    label = _KIND_LABELS_RO.get(kind, "Stare uptime")
    if "down" in kind:
        if error:
            return f"{label}: {target} — {error[:100]}"
        if status_code is not None:
            return f"{label}: {target} (HTTP {status_code})"
        return f"{label}: {target}"
    return f"{label}: {target}"


def record_probe(
    target: str,
    state: str,
    status_code: int | None,
    response_ms: int | None,
    error: str | None,
) -> None:
    """Persist every probe (down-sampling can come later if volume blows up)."""
    global _persist_failures
    try:
        with get_session() as session:
            session.add(UptimeCheck(
                target_url=target.rstrip("/"),
                checked_at=_utcnow(),
                state=state,
                status_code=status_code,
                response_ms=response_ms,
                ok=(state == "up"),
                error=error,
            ))
            session.commit()
    except Exception:
        # #9 — never swallow silently. Log with full context + bump the metric
        # so a sustained DB problem (e.g. lock storm) is visible, not invisible.
        _persist_failures += 1
        log.exception(
            "uptime_check insert FAILED (total failures=%d) target=%s state=%s status=%s",
            _persist_failures, target, state, status_code,
        )


def handle_state_change(
    target: str,
    from_state: str,
    to_state: str,
    status_code: int | None,
    response_ms: int | None,
    error: str | None,
    message: str,
    *,
    kind_for_down: str = "site_down",
    kind_for_slow: str = "site_slow",
    severity_down: str = "critical",
    severity_slow: str = "warn",
) -> None:
    """Translate a watcher state change into an Incident open/close + alert.

    Mapping:
      → down       opens incident category=kind_for_down (default severity critical)
      → slow       opens incident category=kind_for_slow (default severity warn)
      → up         closes whichever was open

    The main watcher (root URL) uses defaults → site_down / site_slow.
    The pages watcher passes kind_for_down='page_down', severity='serious'.
    """
    target = target.rstrip("/")
    now = _utcnow()
    events: list[IncidentAlert] = []

    try:
        with get_session() as session:
            # On any change, close any non-matching open uptime incident for this target.
            for kind, kept_kind in [(kind_for_down, kind_for_down if to_state == "down" else None),
                                    (kind_for_slow, kind_for_slow if to_state == "slow" else None)]:
                if kept_kind == kind:
                    continue
                fp = _fingerprint(target, kind)
                existing = session.scalar(
                    select(Incident).where(
                        Incident.target_url == target,
                        Incident.fingerprint == fp,
                        Incident.resolved_at.is_(None),
                    )
                )
                if existing is not None:
                    existing.resolved_at = now
                    events.append(IncidentAlert(
                        kind="resolved",
                        incident_id=existing.id,
                        fingerprint=existing.fingerprint,
                        category=existing.category,
                        severity=existing.severity,
                        summary=existing.summary,
                        target_url=existing.target_url,
                        page_url=existing.page_url,
                        run_id=0,
                    ))

            # If new state is unhealthy, open (or reopen) the matching incident.
            if to_state in ("down", "slow"):
                kind = kind_for_down if to_state == "down" else kind_for_slow
                severity = severity_down if to_state == "down" else severity_slow
                fp = _fingerprint(target, kind)
                summary = _summary(target, kind, status_code, error)

                existing = session.scalar(
                    select(Incident).where(
                        Incident.target_url == target,
                        Incident.fingerprint == fp,
                    )
                )
                if existing is None:
                    inc = Incident(
                        target_url=target,
                        fingerprint=fp,
                        category=kind,
                        severity=severity,
                        page_url=target,
                        summary=summary[:1024],
                        details={
                            "from_state": from_state,
                            "to_state": to_state,
                            "status_code": status_code,
                            "response_ms": response_ms,
                            "error": error,
                            "message": message,
                        },
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                    session.add(inc)
                    session.flush()
                    events.append(IncidentAlert(
                        kind="new",
                        incident_id=inc.id,
                        fingerprint=inc.fingerprint,
                        category=inc.category,
                        severity=inc.severity,
                        summary=inc.summary,
                        target_url=inc.target_url,
                        page_url=inc.page_url,
                        run_id=0,
                    ))
                else:
                    was_resolved = existing.resolved_at is not None
                    existing.last_seen_at = now
                    existing.summary = summary[:1024]
                    existing.severity = severity
                    if was_resolved:
                        existing.resolved_at = None
                        events.append(IncidentAlert(
                            kind="reopened",
                            incident_id=existing.id,
                            fingerprint=existing.fingerprint,
                            category=existing.category,
                            severity=existing.severity,
                            summary=existing.summary,
                            target_url=existing.target_url,
                            page_url=existing.page_url,
                            run_id=0,
                        ))

            session.commit()
    except Exception:
        global _persist_failures
        _persist_failures += 1
        log.exception("uptime incident upsert FAILED target=%s %s->%s", target, from_state, to_state)
        return

    if events:
        try:
            alert_hub.fire_batch(events)
        except Exception as exc:
            log.warning("uptime alert dispatch failed: %r", exc)


def uptime_percent(target: str, hours: int = 24) -> float | None:
    """% of probes with state == 'up' over the last N hours. None if no data."""
    target = target.rstrip("/")
    since = _utcnow() - timedelta(hours=hours)
    try:
        with get_session() as session:
            total = session.scalar(
                select(sa_func.count(UptimeCheck.id)).where(
                    UptimeCheck.target_url == target,
                    UptimeCheck.checked_at >= since,
                )
            ) or 0
            if total == 0:
                return None
            ok = session.scalar(
                select(sa_func.count(UptimeCheck.id)).where(
                    UptimeCheck.target_url == target,
                    UptimeCheck.checked_at >= since,
                    UptimeCheck.state == "up",
                )
            ) or 0
            return round(ok * 100.0 / total, 2)
    except Exception:
        log.exception("uptime_percent query failed target=%s hours=%d", target, hours)
        return None


def recent_pings(target: str, limit: int = 120) -> list[dict]:
    """Last `limit` probes for a target, oldest→newest, in the shape the live
    sparkline expects ({ts unix, status, response_ms, state, error}).

    DB-backed replacement for the old monitoring.store.recent_pings (#6 — single
    source of truth)."""
    target = target.rstrip("/")
    try:
        with get_session() as session:
            rows = session.scalars(
                select(UptimeCheck)
                .where(UptimeCheck.target_url == target)
                .order_by(UptimeCheck.checked_at.desc())
                .limit(limit)
            ).all()
    except Exception:
        log.exception("recent_pings query failed target=%s", target)
        return []
    out = []
    for r in reversed(rows):  # oldest → newest
        ts = r.checked_at
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "ts": int(ts.timestamp()) if ts else 0,
            "status": r.status_code,
            "response_ms": r.response_ms,
            "state": r.state,
            "error": r.error,
        })
    return out


def prune_old(days: int = 30) -> int:
    """Delete uptime_checks older than N days. Returns count deleted."""
    cutoff = _utcnow() - timedelta(days=days)
    try:
        with get_session() as session:
            result = session.execute(
                delete(UptimeCheck).where(UptimeCheck.checked_at < cutoff)
            )
            session.commit()
            return result.rowcount or 0
    except Exception:
        log.exception("prune_old failed days=%d", days)
        return 0
