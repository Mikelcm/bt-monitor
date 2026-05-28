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
    init_db()
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
    except Exception as exc:
        log.warning("uptime_check insert failed: %r", exc)


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
    init_db()
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
    except Exception as exc:
        log.warning("uptime incident upsert failed: %r", exc)
        return

    if events:
        try:
            alert_hub.fire_batch(events)
        except Exception as exc:
            log.warning("uptime alert dispatch failed: %r", exc)


def uptime_percent(target: str, hours: int = 24) -> float | None:
    """% of probes with state == 'up' over the last N hours. None if no data."""
    init_db()
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
        return None


def prune_old(days: int = 30) -> int:
    """Delete uptime_checks older than N days. Returns count deleted."""
    init_db()
    cutoff = _utcnow() - timedelta(days=days)
    try:
        with get_session() as session:
            result = session.execute(
                delete(UptimeCheck).where(UptimeCheck.checked_at < cutoff)
            )
            session.commit()
            return result.rowcount or 0
    except Exception:
        return 0
