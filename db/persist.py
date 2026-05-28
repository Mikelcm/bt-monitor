"""Persistence layer: convert per-run JSON reports into DB rows.

Lifecycle of an incident (per fingerprint within a target_url):

  Run N    : fingerprint not in DB        -> INSERT incidents (first_seen=now)
                                          -> INSERT observation
  Run N+1  : fingerprint already open     -> UPDATE last_seen, last_seen_run_id
                                          -> INSERT observation
  Run N+2  : fingerprint NOT observed     -> UPDATE resolved_at=now
             (and was previously open)
  Run N+3  : same fingerprint re-appears  -> UPDATE resolved_at=NULL,
             on previously-resolved row      first_seen_at unchanged (history kept)
                                          -> INSERT observation

That gives us "how long was X broken", MTTR per category, and regression
detection (resolved + reopened).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from .models import Incident, IncidentObservation, Run, get_session, init_db
from monitoring.incident_alerts import IncidentAlert


_HERE = Path(__file__).resolve().parent.parent
_DATA = _HERE / "data"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load(name: str) -> dict:
    p = _DATA / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _truncate(s: Any, n: int = 100) -> str:
    if not isinstance(s, str):
        s = str(s) if s is not None else ""
    s = s.strip().replace("\n", " ")
    return s[:n]


def _findings_from_reports() -> list[dict]:
    """Extract a flat list of {fingerprint, category, severity, page_url,
    summary, details} from every per-check JSON report.

    One entry == one distinct issue. Deduplication happens at the DB level
    via the (target_url, fingerprint) unique constraint.
    """
    findings: list[dict] = []

    # --- broken links ---
    links = _load("links_report.json")
    for b in links.get("broken", []) or []:
        url = b.get("url") or ""
        status = b.get("status")
        err = b.get("error")
        fp = f"broken_link|{url}"
        findings.append({
            "fingerprint": fp,
            "category": "broken_link",
            "severity": "serious",
            "page_url": None,
            "summary": f"Broken link: {url}" + (f" (HTTP {status})" if status else (f" ({_truncate(err, 60)})" if err else "")),
            "details": b,
        })

    # --- slow pages ---
    perf = _load("performance_report.json")
    threshold = perf.get("threshold_ms")
    for s in perf.get("slow_pages", []) or []:
        page = s.get("page_url") or ""
        wall = s.get("wall_time_ms")
        fp = f"slow_page|{page}"
        findings.append({
            "fingerprint": fp,
            "category": "slow_page",
            "severity": "moderate",
            "page_url": page,
            "summary": f"Slow page: {page} took {wall} ms (threshold {threshold} ms)",
            "details": s,
        })

    # --- asset failures ---
    assets = _load("assets_report.json")
    for entry in assets.get("per_page", []) or []:
        page = entry.get("page_url") or ""
        for f in entry.get("failed", []) or []:
            url = f.get("url") or ""
            rtype = f.get("resource_type") or "asset"
            failure_text = f.get("failure_text") or ""
            status = f.get("status")
            # Include failure_text so 2 entries for same URL with different
            # statuses (e.g. 404 vs ERR_ABORTED) are tracked separately.
            fp = f"asset_failure|{page}|{url}|{rtype}|{status}|{failure_text}"
            findings.append({
                "fingerprint": fp,
                "category": "asset_failure",
                "severity": "moderate",
                "page_url": page,
                "summary": f"Missing {rtype}: {url}" + (f" (HTTP {status})" if status else (f" ({_truncate(failure_text, 60)})" if failure_text else "")),
                "details": f | {"page_url": page},
            })

    # --- form issues (one incident per issue per form) ---
    forms = _load("forms_report.json")
    for form in forms.get("forms", []) or []:
        page = form.get("page_url") or ""
        idx = form.get("form_index", 0)
        for issue in form.get("issues", []) or []:
            fp = f"form_issue|{page}|{idx}|{issue}"
            findings.append({
                "fingerprint": fp,
                "category": "form_issue",
                "severity": "serious" if "action_returns" in issue or "no_submit" in issue else "moderate",
                "page_url": page,
                "summary": f"Form #{idx} on {page}: {issue}",
                "details": {"issue": issue, "form": form},
            })

    # --- doc leaks (CRITICAL — bank-grade severity) ---
    docs = _load("docs_report.json")
    for leak in docs.get("leaks", []) or []:
        url = leak.get("url") or ""
        markers = [m.get("marker") for m in (leak.get("markers_found") or [])]
        fp = f"doc_leak|{url}"
        findings.append({
            "fingerprint": fp,
            "category": "doc_leak",
            "severity": "critical",
            "page_url": None,
            "summary": f"Internal document publicly accessible: {url} (markers: {', '.join(markers)})",
            "details": leak,
        })

    # --- accessibility (axe-core violations) ---
    a11y = _load("accessibility_report.json")
    for page in a11y.get("pages", []) or []:
        page_url = page.get("page_url") or ""
        if page.get("error") == "axe_not_loaded":
            fp = f"a11y_error|{page_url}|axe_not_loaded"
            findings.append({
                "fingerprint": fp,
                "category": "a11y_error",
                "severity": "minor",
                "page_url": page_url,
                "summary": f"axe-core failed to load on {page_url}",
                "details": {"error": "axe_not_loaded"},
            })
            continue
        for v in page.get("violations", []) or []:
            rule_id = v.get("id") or "unknown"
            impact = (v.get("impact") or "moderate").lower()
            # one incident per node target (the specific element on the page)
            nodes = v.get("nodes", []) or [{}]
            for node in nodes:
                target = "|".join(node.get("target") or [])
                fp = f"a11y|{page_url}|{rule_id}|{target}"
                findings.append({
                    "fingerprint": fp,
                    "category": "a11y",
                    "severity": impact if impact in ("critical", "serious", "moderate", "minor") else "moderate",
                    "page_url": page_url,
                    "summary": f"A11y [{impact}] {rule_id} on {page_url}",
                    "details": {"rule": rule_id, "impact": impact, "node": node, "help_url": v.get("helpUrl")},
                })

    # --- SEO ---
    seo = _load("seo_report.json")
    for page in seo.get("pages", []) or []:
        page_url = page.get("page_url") or ""
        for issue in page.get("issues", []) or []:
            fp = f"seo|{page_url}|{issue}"
            findings.append({
                "fingerprint": fp,
                "category": "seo",
                "severity": "minor",
                "page_url": page_url,
                "summary": f"SEO issue on {page_url}: {issue}",
                "details": {"issue": issue, "findings": page.get("findings")},
            })

    # --- console (JS errors + failed requests) ---
    console = _load("console_report.json")
    for page in console.get("pages", []) or []:
        page_url = page.get("page_url") or ""
        for je in page.get("js_errors", []) or []:
            text = _truncate(je.get("text") or je.get("message") or je, 120)
            fp = f"js_error|{page_url}|{text}"
            findings.append({
                "fingerprint": fp,
                "category": "js_error",
                "severity": "serious",
                "page_url": page_url,
                "summary": f"JS error on {page_url}: {text}",
                "details": je,
            })
        for ce in page.get("console_errors", []) or []:
            text = _truncate(ce.get("text") or ce, 120)
            fp = f"console_error|{page_url}|{text}"
            findings.append({
                "fingerprint": fp,
                "category": "console_error",
                "severity": "moderate",
                "page_url": page_url,
                "summary": f"console.error on {page_url}: {text}",
                "details": ce,
            })
        for fr in page.get("failed_requests", []) or []:
            url = fr.get("url") or ""
            failure = fr.get("failure") or ""
            fp = f"failed_request|{page_url}|{url}|{failure}"
            findings.append({
                "fingerprint": fp,
                "category": "failed_request",
                "severity": "moderate",
                "page_url": page_url,
                "summary": f"Failed request on {page_url}: {url} ({failure})",
                "details": fr,
            })

    return findings


def persist_run(target_url: str, summary: dict, state: dict | None = None) -> dict:
    """Insert a Run row, upsert incidents from the latest JSON reports,
    resolve any open incidents not observed this run, and emit an observation
    for every still-open incident.

    Returns a small dict with stats: {run_id, new, still_open, resolved}.
    """
    init_db()
    now = _utcnow()

    findings = _findings_from_reports()
    # In-run dedup: same fingerprint may appear twice in the same report set
    # (e.g. asset URL listed in both assets_report.failed and console.failed_requests).
    seen_fps: dict[str, dict] = {}
    for f in findings:
        seen_fps.setdefault(f["fingerprint"], f)

    stats = {"new": 0, "still_open": 0, "reopened": 0, "resolved": 0}
    events: list[IncidentAlert] = []

    def _emit(kind: str, inc: Incident, run_id: int) -> None:
        events.append(IncidentAlert(
            kind=kind,
            incident_id=inc.id,
            fingerprint=inc.fingerprint,
            category=inc.category,
            severity=inc.severity,
            summary=inc.summary,
            target_url=inc.target_url,
            page_url=inc.page_url,
            run_id=run_id,
        ))

    with get_session() as session:
        # 1. Create the Run row.
        started = None
        finished = None
        if state:
            try:
                started = datetime.fromisoformat(state.get("started_at")) if state.get("started_at") else None
                finished = datetime.fromisoformat(state.get("finished_at")) if state.get("finished_at") else None
            except Exception:
                started = finished = None
        run = Run(
            base_url=target_url,
            started_at=started or now,
            finished_at=finished or now,
            status=str(summary.get("status") or "unknown"),
            health_score=summary.get("health_score") if isinstance(summary.get("health_score"), int) else None,
            duration_s=(state or {}).get("total_duration_s"),
            summary=summary,
        )
        session.add(run)
        session.flush()  # we need run.id below

        # 2. Upsert each finding.
        for fp, f in seen_fps.items():
            existing: Incident | None = session.scalar(
                select(Incident).where(
                    Incident.target_url == target_url,
                    Incident.fingerprint == fp,
                )
            )
            if existing is None:
                inc = Incident(
                    target_url=target_url,
                    fingerprint=fp,
                    category=f["category"],
                    severity=f["severity"],
                    page_url=f.get("page_url"),
                    summary=f["summary"][:1024],
                    details=f.get("details"),
                    first_seen_at=now,
                    last_seen_at=now,
                    first_seen_run_id=run.id,
                    last_seen_run_id=run.id,
                )
                session.add(inc)
                session.flush()
                session.add(IncidentObservation(incident_id=inc.id, run_id=run.id, observed_at=now))
                stats["new"] += 1
                _emit("new", inc, run.id)
            else:
                was_resolved = existing.resolved_at is not None
                existing.last_seen_at = now
                existing.last_seen_run_id = run.id
                # refresh details + summary in case the data changed (e.g. status code shifted)
                existing.summary = f["summary"][:1024]
                existing.details = f.get("details")
                existing.severity = f["severity"]
                if was_resolved:
                    existing.resolved_at = None
                    stats["reopened"] += 1
                    _emit("reopened", existing, run.id)
                else:
                    stats["still_open"] += 1
                session.add(IncidentObservation(incident_id=existing.id, run_id=run.id, observed_at=now))

        # 3. Resolve open incidents not observed this run (for this target only).
        observed_fps = set(seen_fps.keys())
        open_for_target: Iterable[Incident] = session.scalars(
            select(Incident).where(
                Incident.target_url == target_url,
                Incident.resolved_at.is_(None),
                Incident.last_seen_run_id != run.id,
            )
        )
        for inc in open_for_target:
            if inc.fingerprint not in observed_fps:
                inc.resolved_at = now
                stats["resolved"] += 1
                _emit("resolved", inc, run.id)

        session.commit()
        stats["run_id"] = run.id

    stats["events"] = events
    return stats
