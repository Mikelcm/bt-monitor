"""FastAPI dashboard — v3 (sidebar layout, sectionized pages).

Pages:
    /              -> Privire generală (overview)
    /incidents     -> Incidente (filtrabile)
    /history       -> Istoric scan-uri
    /history/{id}  -> Detaliu scan
    /live          -> Monitorizare live (watcher uptime)
    /reports       -> Rapoarte / Export
    /settings      -> Setări (țintă, alerte, DB)

Exports:
    /export/incidents.csv
    /export/bt-monitor.xlsx
    /export/audit.pdf

JSON APIs:
    /api/state         deep-scan run state
    /api/reports       all per-check JSON reports
    /api/incidents     filterable incident list
    /api/incidents/{id}  incident detail + observations
    /api/runs          run history
    /api/alerts        alert audit log
    /api/uptime        live watcher state

Mutations:
    POST /run          kick deep-scan subprocess
    POST /target       change scan target
    POST /clear        wipe per-check JSON reports
    POST /live/add     add a live-watch target
    POST /live/remove  remove a live-watch target
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func as sa_func, select

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import (
    DATA_DIR, get_base_url, set_base_url, get_recent_targets,
    get_target_changed_at,
    get_live_targets, add_live_target, remove_live_target, all_watched_targets,
)
from runner import read_state, build_summary
from monitoring.alerts import AlertHub
from monitoring.watcher import Watcher
from monitoring.pages_watcher import PagesWatcher, PAGES_INTERVAL_S
from monitoring.incident_alerts import hub as incident_alert_hub
from monitoring.uptime_persist import (
    prune_old as prune_uptime_checks, uptime_percent, recent_pings,
)
from db.models import Alert, Incident, IncidentObservation, Run, UptimeCheck, get_session, init_db
from dashboard.exports import build_incidents_csv, build_incidents_xlsx, build_audit_pdf
from utils.time_ro import format_ro, format_ro_short, humanize_duration, to_ro
from utils.logging_setup import configure_logging

configure_logging()  # #22 — uniform, env-driven logging for the whole process

DATA = ROOT / DATA_DIR
DASH_DIR = ROOT / "dashboard"

# ---------------------------------------------------------------------
# global singletons (one per process)
# ---------------------------------------------------------------------
_alerts = AlertHub()
_watcher = Watcher(alerts=_alerts)
_pages_watcher = PagesWatcher()
_watcher_task: asyncio.Task | None = None
_pages_task: asyncio.Task | None = None
_scheduler_task: asyncio.Task | None = None
_scheduler_stop = asyncio.Event()

# #5 — single-flight + cooldown for the (expensive) deep scan. Prevents a flood
# of /run clicks (or a CSRF/replay) from spawning many headless-browser scans
# and exhausting the host. Min interval between starts is env-tunable.
_scan_lock = asyncio.Lock()
_scan_in_flight = False
_last_scan_start = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Default: every 6h. Set BT_MONITOR_SCAN_INTERVAL_H=0 to disable.
SCAN_INTERVAL_H = _env_float("BT_MONITOR_SCAN_INTERVAL_H", 6.0)
PRUNE_INTERVAL_H = _env_float("BT_MONITOR_PRUNE_INTERVAL_H", 24.0)
UPTIME_RETENTION_DAYS = int(_env_float("BT_MONITOR_UPTIME_RETENTION_DAYS", 30))


async def _scheduler_loop():
    """Auto-kick a deep scan every SCAN_INTERVAL_H hours. Also prunes
    old uptime_checks once per PRUNE_INTERVAL_H. No-op if interval is 0."""
    if SCAN_INTERVAL_H <= 0:
        print("[scheduler] disabled (BT_MONITOR_SCAN_INTERVAL_H=0)", flush=True)
        return
    print(f"[scheduler] active — deep scan every {SCAN_INTERVAL_H}h", flush=True)
    last_prune_ts = 0.0
    # Small grace period at startup so we don't immediately fire on boot.
    try:
        await asyncio.wait_for(_scheduler_stop.wait(), timeout=60)
        return
    except asyncio.TimeoutError:
        pass
    while not _scheduler_stop.is_set():
        try:
            # Check last run time for the active target.
            init_db()
            target = get_base_url().rstrip("/")
            now = datetime.now(timezone.utc)
            with get_session() as session:
                last_run = session.scalar(
                    select(Run)
                    .where(Run.base_url == target)
                    .order_by(desc(Run.started_at))
                    .limit(1)
                )
            should_run = True
            if last_run and last_run.started_at:
                last_started = last_run.started_at
                if last_started.tzinfo is None:
                    last_started = last_started.replace(tzinfo=timezone.utc)
                age_h = (now - last_started).total_seconds() / 3600.0
                if age_h < SCAN_INTERVAL_H:
                    should_run = False
            state = read_state()
            if state.get("status") == "running":
                should_run = False

            if should_run:
                started, reason = await _try_start_scan()
                if started:
                    print(f"[scheduler] firing deep scan (last was >{SCAN_INTERVAL_H}h ago)", flush=True)
                else:
                    print(f"[scheduler] skip scan: {reason}", flush=True)

            # Prune uptime_checks periodically.
            now_ts = now.timestamp()
            if now_ts - last_prune_ts > PRUNE_INTERVAL_H * 3600:
                deleted = prune_uptime_checks(days=UPTIME_RETENTION_DAYS)
                if deleted:
                    print(f"[scheduler] pruned {deleted} uptime_check rows >{UPTIME_RETENTION_DAYS}d old", flush=True)
                last_prune_ts = now_ts
        except Exception as exc:
            print(f"[scheduler] tick error: {exc!r}", flush=True)

        # Sleep until next check (every minute).
        try:
            await asyncio.wait_for(_scheduler_stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _watcher_task, _pages_task, _scheduler_task
    init_db()
    _watcher.set_targets(all_watched_targets())
    _watcher_task = asyncio.create_task(_watcher.run_forever())
    _pages_task = asyncio.create_task(_pages_watcher.run_forever())
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    try:
        yield
    finally:
        # #16 — graceful shutdown: signal loops to stop, await them, then
        # hard-cancel any that overran and await the cancellation so no task
        # is left dangling (clean for tests + reloads).
        _scheduler_stop.set()
        await _watcher.stop()
        await _pages_watcher.stop()
        for task in (_watcher_task, _pages_task, _scheduler_task):
            if not task:
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


app = FastAPI(title="BT Monitor Dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory=str(DASH_DIR / "templates"))
# Romania-time formatting filters (DD.MM.YYYY HH:MM:SS) for all templates.
templates.env.filters["dt_ro"] = format_ro
templates.env.filters["dt_ro_short"] = format_ro_short
templates.env.filters["humanize_duration"] = humanize_duration
app.mount("/static", StaticFiles(directory=str(DASH_DIR / "static")), name="static")

# ---------------------------------------------------------------------
# #1 Basic Auth — protect every route (incl. the state-mutating POSTs).
# Enforced ONLY when credentials are configured via env. Production MUST set
# them; local dev may omit them (a loud warning is logged at startup).
# ---------------------------------------------------------------------
import base64 as _b64
import hashlib as _hashlib
import secrets as _secrets
from urllib.parse import quote as _quote
from fastapi.responses import Response as _Response

_AUTH_USER = os.environ.get("BT_MONITOR_AUTH_USER")
_AUTH_PASS = os.environ.get("BT_MONITOR_AUTH_PASS")
_AUTH_ENABLED = bool(_AUTH_USER and _AUTH_PASS)
_AUTH_EXEMPT = ("/static", "/healthz", "/login", "/logout")

# Signing key for session cookies. Prefer an explicit secret; otherwise derive a
# stable one from the credentials so sessions survive restarts without extra
# config. When auth is off (dev) the key is irrelevant.
_SESSION_SECRET = os.environ.get("BT_MONITOR_SECRET_KEY") or (
    _hashlib.sha256(f"{_AUTH_USER}:{_AUTH_PASS}:btmonitor".encode()).hexdigest()
    if _AUTH_ENABLED else "dev-insecure-no-auth"
)

if not _AUTH_ENABLED:
    logging.getLogger("dashboard").warning(
        "AUTH DISABLED — set BT_MONITOR_AUTH_USER and BT_MONITOR_AUTH_PASS to "
        "require login. Do NOT expose the dashboard publicly without them."
    )


def _basic_ok(request) -> bool:
    """True if the request carries a valid HTTP Basic header (for automation)."""
    hdr = request.headers.get("Authorization", "")
    if not hdr.startswith("Basic "):
        return False
    try:
        user, _, pw = _b64.b64decode(hdr[6:]).decode("utf-8", "ignore").partition(":")
        return _secrets.compare_digest(user, _AUTH_USER or "") and _secrets.compare_digest(pw, _AUTH_PASS or "")
    except Exception:
        return False


@app.middleware("http")
async def _auth_gate(request, call_next):
    path = request.url.path
    if not _AUTH_ENABLED or path.startswith(_AUTH_EXEMPT):
        return await call_next(request)
    # 1) signed session cookie (the human login flow)
    try:
        if request.session.get("user"):
            return await call_next(request)
    except Exception:
        pass
    # 2) HTTP Basic header (automation / API clients)
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Basic "):
        if _basic_ok(request):
            return await call_next(request)
        return _Response("Unauthorized", status_code=401,
                         headers={"WWW-Authenticate": 'Basic realm="BT Monitor"'})
    # 3) unauthenticated: APIs/exports get 401, browser pages go to the login form
    if path.startswith(("/api", "/export")):
        return _Response("Unauthorized", status_code=401)
    return RedirectResponse(f"/login?next={_quote(path)}", status_code=303)


# ---------------------------------------------------------------------
# #4 CSRF — same-origin check on state-changing requests. The dashboard's
# own <form>s post same-origin (Origin/Referer match the Host). A cross-site
# request forged in a victim's browser carries a foreign Origin/Referer, which
# we reject. Requests with neither header (curl, server-to-server) are allowed
# — they aren't the ambient-credential CSRF vector this defends against.
# ---------------------------------------------------------------------
from urllib.parse import urlparse as _urlparse

_CSRF_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _csrf_same_origin(request, call_next):
    if request.method in _CSRF_METHODS:
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        src = origin or referer
        if src:
            src_netloc = _urlparse(src).netloc
            if src_netloc and host and src_netloc != host:
                logging.getLogger("dashboard").warning(
                    "CSRF blocked: %s %s origin/referer=%s host=%s",
                    request.method, request.url.path, src_netloc, host,
                )
                return _Response("CSRF check failed", status_code=403)
    return await call_next(request)


# SessionMiddleware must wrap the auth/csrf middlewares so request.session is
# available inside them. Added LAST → outermost in the stack → runs first.
from starlette.middleware.sessions import SessionMiddleware as _SessionMiddleware

app.add_middleware(
    _SessionMiddleware,
    secret_key=_SESSION_SECRET,
    session_cookie="btm_session",
    same_site="strict",
    https_only=False,   # set true behind TLS; the reverse proxy terminates HTTPS
    max_age=60 * 60 * 12,   # 12h
)

# Expose auth state to all templates.
templates.env.globals["auth_enabled"] = _AUTH_ENABLED


# ---------------------------------------------------------------------
# Login / logout (session-based human auth; Basic still works for automation)
# ---------------------------------------------------------------------
def _safe_next(nxt: str | None) -> str:
    """Only allow local redirects (avoid open-redirect via ?next=)."""
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return "/"


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    if not _AUTH_ENABLED or request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"next": _safe_next(next), "error": None},
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    if (_AUTH_ENABLED
            and _secrets.compare_digest(username, _AUTH_USER or "")
            and _secrets.compare_digest(password, _AUTH_PASS or "")):
        request.session["user"] = username
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"next": _safe_next(next), "error": "Utilizator sau parolă incorecte."},
        status_code=401,
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
async def healthz():
    """Unauthenticated liveness probe (for Docker/reverse-proxy healthchecks)."""
    return {"status": "ok"}


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _load_json(name: str) -> dict | None:
    p = DATA / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


REPORT_FILES = {
    "sitemap":       "sitemap_pages.json",
    "links":         "links_report.json",
    "performance":   "performance_report.json",
    "assets":        "assets_report.json",
    "forms":         "forms_report.json",
    "docs":          "docs_report.json",
    "accessibility": "accessibility_report.json",
    "seo":           "seo_report.json",
    "console":       "console_report.json",
}

SEVERITY_ORDER = {"critical": 0, "serious": 1, "moderate": 2, "minor": 3}


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _open_count_for(target: str) -> int:
    try:
        with get_session() as session:
            return session.scalar(
                select(sa_func.count(Incident.id)).where(
                    Incident.target_url == target.rstrip("/"),
                    Incident.resolved_at.is_(None),
                )
            ) or 0
    except Exception:
        return 0


def _serialize_incident(inc: Incident) -> dict:
    return {
        "id": inc.id,
        "target_url": inc.target_url,
        "fingerprint": inc.fingerprint,
        "category": inc.category,
        "severity": inc.severity,
        "page_url": inc.page_url,
        "summary": inc.summary,
        "first_seen_at": _ensure_utc(inc.first_seen_at).isoformat() if inc.first_seen_at else None,
        "last_seen_at": _ensure_utc(inc.last_seen_at).isoformat() if inc.last_seen_at else None,
        "resolved_at": _ensure_utc(inc.resolved_at).isoformat() if inc.resolved_at else None,
        "is_open": inc.is_open,
        "first_seen_run_id": inc.first_seen_run_id,
        "last_seen_run_id": inc.last_seen_run_id,
    }


def _serialize_run(run: Run) -> dict:
    return {
        "id": run.id,
        "base_url": run.base_url,
        "started_at": _ensure_utc(run.started_at).isoformat() if run.started_at else None,
        "finished_at": _ensure_utc(run.finished_at).isoformat() if run.finished_at else None,
        "status": run.status,
        "health_score": run.health_score,
        "duration_s": run.duration_s,
    }


def _enrich_incident_for_ui(inc: Incident, now: datetime) -> dict:
    first = _ensure_utc(inc.first_seen_at)
    last = _ensure_utc(inc.last_seen_at)
    resolved = _ensure_utc(inc.resolved_at)
    age_secs = ((resolved or now) - first).total_seconds() if first else 0
    return {
        "id": inc.id,
        "category": inc.category,
        "severity": inc.severity,
        "page_url": inc.page_url,
        "summary": inc.summary,
        "first_seen_at": first,
        "last_seen_at": last,
        "resolved_at": resolved,
        "age_days": round(age_secs / 86400, 1),
        "is_open": inc.is_open,
    }


def _build_chart_points(runs: list[Run]) -> list[dict]:
    """Sparkline points for a list of runs (oldest→newest)."""
    if not runs:
        return []
    valid = [r.health_score for r in runs if r.health_score is not None]
    max_s = max(valid) if valid else 100
    min_s = min(valid) if valid else 0
    span = max(1, max_s - min_s)
    out = []
    for i, r in enumerate(runs):
        x = (i / max(1, len(runs) - 1)) * 100
        if r.health_score is None:
            y = 50
        else:
            y = 100 - ((r.health_score - min_s) / span) * 100
        out.append({
            "x": round(x, 2),
            "y": round(y, 2),
            "score": r.health_score,
            "status": r.status,
            "started": _ensure_utc(r.started_at).isoformat() if r.started_at else "",
            "run_id": r.id,
        })
    return out


def _global_status(base_url: str) -> dict:
    """Build the persistent status-bar context shown on every page.

    Returns: {tone, led_state, title, detail, open_critical, last_scan_text}
    where tone ∈ {ok, warn, critical, muted}.
    """
    target = base_url.rstrip("/")
    target_state = _watcher.targets.get(target)
    live_state = target_state.current_state if target_state else "unknown"

    init_db()
    open_critical = 0
    last_run = None
    try:
        with get_session() as session:
            open_critical = session.scalar(
                select(sa_func.count(Incident.id)).where(
                    Incident.target_url == target,
                    Incident.resolved_at.is_(None),
                    Incident.severity == "critical",
                )
            ) or 0
            last_run = session.scalar(
                select(Run).where(Run.base_url == target)
                .order_by(desc(Run.started_at)).limit(1)
            )
    except Exception:
        pass

    last_scan_text = None
    if last_run and last_run.started_at:
        dt = _ensure_utc(last_run.started_at)
        if dt is not None:
            secs = (datetime.now(timezone.utc) - dt).total_seconds()
            if secs < 60:
                last_scan_text = f"acum {int(secs)}s"
            elif secs < 3600:
                last_scan_text = f"acum {int(secs/60)} min"
            elif secs < 86400:
                last_scan_text = f"acum {int(secs/3600)}h"
            else:
                last_scan_text = f"acum {int(secs/86400)}z"

    if live_state == "down":
        return {
            "tone": "critical", "led_state": "down",
            "title": "Site INDISPONIBIL",
            "detail": f"watcher-ul nu poate ajunge la {target}",
            "open_critical": open_critical,
            "last_scan_text": last_scan_text,
        }
    if open_critical > 0:
        return {
            "tone": "critical", "led_state": "down",
            "title": f"{open_critical} incidente critice deschise",
            "detail": "necesită atenție imediată",
            "open_critical": open_critical,
            "last_scan_text": last_scan_text,
        }
    if live_state == "slow":
        return {
            "tone": "warn", "led_state": "slow",
            "title": "Site lent",
            "detail": "răspuns peste pragul de 3s",
            "open_critical": 0,
            "last_scan_text": last_scan_text,
        }
    if live_state == "up":
        return {
            "tone": "ok", "led_state": "up",
            "title": "Toate sistemele operaționale",
            "detail": f"site verificat la fiecare 30s · {target}",
            "open_critical": 0,
            "last_scan_text": last_scan_text,
        }
    return {
        "tone": "muted", "led_state": "unknown",
        "title": "Verificare în curs…",
        "detail": "watcher-ul rulează primul probe",
        "open_critical": 0,
        "last_scan_text": last_scan_text,
    }


def _live_snapshot(base_url: str) -> dict:
    target = base_url.rstrip("/")
    target_state = _watcher.targets.get(target)
    pings = recent_pings(target, limit=120)
    # DB-backed SLA windows (None if no data yet)
    sla_24h = uptime_percent(target, hours=24)
    sla_7d = uptime_percent(target, hours=24 * 7)
    sla_30d = uptime_percent(target, hours=24 * 30)
    uptime_24h = sla_24h
    alerts = [
        {
            "ts": e.ts, "url": e.url, "from": e.from_state, "to": e.to_state,
            "message": e.message, "severity": e.severity,
            "status": e.status, "response_ms": e.response_ms,
        }
        for e in _alerts.recent()
    ]
    return {
        "target": base_url,
        "state": (target_state.current_state if target_state else "unknown"),
        "last_status": (target_state.last_status if target_state else None),
        "last_response_ms": (target_state.last_response_ms if target_state else None),
        "last_seen_ts": (target_state.last_seen_ts if target_state else 0),
        "last_error": (target_state.last_error if target_state else None),
        "pings": pings,
        "uptime_24h_pct": uptime_24h,
        "sla_24h": sla_24h,
        "sla_7d": sla_7d,
        "sla_30d": sla_30d,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def overview(request: Request):
    base_url = get_base_url()
    target = base_url.rstrip("/")
    summary = _load_json("summary.json") or build_summary()
    run_state = read_state()
    scan_running = run_state.get("status") == "running"
    has_data = bool(summary.get("pages")) and summary.get("base_url") == base_url

    # criticals (doc leaks etc.)
    criticals = []
    docs = _load_json("docs_report.json") or {}
    for d in docs.get("docs", []):
        if d.get("markers_found"):
            criticals.append({
                "type": "doc_leak",
                "title": "Document intern accesibil public",
                "url": d["url"],
                "detail": ", ".join(m["marker"] for m in d["markers_found"][:3]),
            })

    # DB aggregates
    now = datetime.now(timezone.utc)
    init_db()
    with get_session() as session:
        sev_counts_rows = session.execute(
            select(Incident.severity, sa_func.count(Incident.id))
            .where(Incident.target_url == target, Incident.resolved_at.is_(None))
            .group_by(Incident.severity)
        ).all()
        sev_counts = {sev: cnt for sev, cnt in sev_counts_rows}
        open_total = sum(sev_counts.values())
        runs_count = session.scalar(
            select(sa_func.count(Run.id)).where(Run.base_url == target)
        ) or 0
        recent_runs = list(reversed(session.scalars(
            select(Run).where(Run.base_url == target)
            .order_by(desc(Run.started_at)).limit(30)
        ).all()))
        top_inc_rows = session.scalars(
            select(Incident).where(
                Incident.target_url == target,
                Incident.resolved_at.is_(None),
            )
        ).all()

    chart_points = _build_chart_points(recent_runs)

    # Activity feed: recent incidents (new + resolved) + recent runs.
    activity: list[dict] = []
    with get_session() as session:
        recent_open = session.scalars(
            select(Incident).where(Incident.target_url == target)
            .order_by(desc(Incident.first_seen_at)).limit(10)
        ).all()
        recent_resolved = session.scalars(
            select(Incident).where(
                Incident.target_url == target,
                Incident.resolved_at.is_not(None),
            ).order_by(desc(Incident.resolved_at)).limit(10)
        ).all()
    for inc in recent_open:
        activity.append({
            "kind": "incident_open",
            "tone": "critical" if inc.severity == "critical" else ("warn" if inc.severity == "serious" else "info"),
            "icon": "alert",
            "title": f"Incident nou: {inc.category}",
            "sub": inc.summary[:120],
            "when": _ensure_utc(inc.first_seen_at),
        })
    for inc in recent_resolved:
        activity.append({
            "kind": "incident_resolved",
            "tone": "ok",
            "icon": "check",
            "title": f"Rezolvat: {inc.category}",
            "sub": inc.summary[:120],
            "when": _ensure_utc(inc.resolved_at),
        })
    for r in recent_runs[-5:]:
        if r.started_at:
            activity.append({
                "kind": "scan",
                "tone": "info",
                "icon": "history",
                "title": f"Scan #{r.id} finalizat",
                "sub": f"{r.status} · health score {r.health_score if r.health_score is not None else '—'}",
                "when": _ensure_utc(r.finished_at or r.started_at),
            })
    activity = [a for a in activity if a["when"] is not None]
    activity.sort(key=lambda a: a["when"], reverse=True)
    activity = activity[:15]
    # add a 'when_relative' string for display
    now_for_act = datetime.now(timezone.utc)
    for a in activity:
        secs = (now_for_act - a["when"]).total_seconds()
        if secs < 60:
            a["when_str"] = f"acum {int(secs)}s"
        elif secs < 3600:
            a["when_str"] = f"acum {int(secs/60)} min"
        elif secs < 86400:
            a["when_str"] = f"acum {int(secs/3600)}h"
        else:
            a["when_str"] = f"acum {int(secs/86400)}z"

    # top 5 by severity then most recent
    enriched = [_enrich_incident_for_ui(i, now) for i in top_inc_rows]
    enriched.sort(key=lambda it: (
        SEVERITY_ORDER.get(it["severity"], 9),
        -(it["last_seen_at"].timestamp() if it["last_seen_at"] else 0),
    ))
    top_incidents = enriched[:5]

    last_run_at = None
    last_run_duration = None
    if recent_runs:
        last = recent_runs[-1]
        if last.started_at:
            last_run_at = _ensure_utc(last.started_at)
        last_run_duration = last.duration_s

    live = _live_snapshot(base_url)

    return templates.TemplateResponse(
        request=request,
        name="overview.html",
        context={
            "active": "overview",
            "base_url": base_url,
            "nav_open_count": open_total,
            "summary": summary,
            "scan_running": scan_running,
            "run_state": run_state,
            "has_data": has_data,
            "criticals": criticals,
            "sev_counts": sev_counts,
            "open_total": open_total,
            "runs_count": runs_count,
            "chart_points": chart_points,
            "top_incidents": top_incidents,
            "last_run_at": last_run_at,
            "last_run_duration": last_run_duration,
            "live": live,
            "activity": activity,
            "global_status": _global_status(base_url),
        },
    )


@app.get("/incidents", response_class=HTMLResponse)
async def incidents_page(
    request: Request,
    status: str = "open",
    target_url: str | None = None,
    category: str | None = None,
    severity: str | None = None,
):
    init_db()
    base_url = get_base_url()
    target = (target_url or base_url).rstrip("/")

    with get_session() as session:
        stmt = select(Incident).where(Incident.target_url == target)
        if status == "open":
            stmt = stmt.where(Incident.resolved_at.is_(None))
        elif status == "resolved":
            stmt = stmt.where(Incident.resolved_at.is_not(None))
        if category:
            stmt = stmt.where(Incident.category == category)
        if severity:
            stmt = stmt.where(Incident.severity == severity)
        stmt = stmt.order_by(desc(Incident.last_seen_at)).limit(500)
        incidents = session.scalars(stmt).all()

        sev_counts_rows = session.execute(
            select(Incident.severity, sa_func.count(Incident.id))
            .where(Incident.target_url == target, Incident.resolved_at.is_(None))
            .group_by(Incident.severity)
        ).all()
        sev_counts = {sev: cnt for sev, cnt in sev_counts_rows}

        cat_counts = sorted(session.execute(
            select(Incident.category, sa_func.count(Incident.id))
            .where(Incident.target_url == target, Incident.resolved_at.is_(None))
            .group_by(Incident.category)
        ).all(), key=lambda r: -r[1])

        recent_runs = list(reversed(session.scalars(
            select(Run).where(Run.base_url == target)
            .order_by(desc(Run.started_at)).limit(30)
        ).all()))

    chart_points = _build_chart_points(recent_runs)

    now = datetime.now(timezone.utc)
    items = [_enrich_incident_for_ui(i, now) for i in incidents]
    items.sort(key=lambda it: (
        SEVERITY_ORDER.get(it["severity"], 9),
        -(it["last_seen_at"].timestamp() if it["last_seen_at"] else 0),
    ))

    return templates.TemplateResponse(
        request=request,
        name="incidents.html",
        context={
            "active": "incidents",
            "base_url": base_url,
            "target_url": target,
            "status": status,
            "category": category,
            "severity": severity,
            "items": items,
            "total": len(items),
            "sev_counts": sev_counts,
            "cat_counts": cat_counts,
            "chart_points": chart_points,
            "nav_open_count": sum(sev_counts.values()),
        },
    )


@app.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    init_db()
    base_url = get_base_url()
    with get_session() as session:
        runs = [_serialize_run(r) for r in session.scalars(
            select(Run).order_by(desc(Run.started_at)).limit(500)
        ).all()]
    return templates.TemplateResponse(
        request=request,
        name="history.html",
        context={
            "active": "history",
            "base_url": base_url,
            "runs": runs,
            "nav_open_count": _open_count_for(base_url),
            "global_status": _global_status(base_url),
        },
    )


@app.get("/history/{run_id}", response_class=HTMLResponse)
async def history_detail(request: Request, run_id: int):
    init_db()
    base_url = get_base_url()
    now = datetime.now(timezone.utc)
    with get_session() as session:
        run = session.get(Run, run_id)
        if run is None:
            return HTMLResponse("Run not found", status_code=404)
        obs_rows = session.scalars(
            select(IncidentObservation).where(IncidentObservation.run_id == run_id)
        ).all()
        incident_ids = [o.incident_id for o in obs_rows]
        if incident_ids:
            incidents = session.scalars(
                select(Incident).where(Incident.id.in_(incident_ids))
            ).all()
        else:
            incidents = []
        # detach to avoid lazy-load surprises in template
        observed = []
        for inc in incidents:
            d = _enrich_incident_for_ui(inc, now)
            d["first_seen_at_str"] = inc.first_seen_at.isoformat() if inc.first_seen_at else None
            observed.append({
                **d,
                "first_seen_at": d["first_seen_at"].isoformat() if d["first_seen_at"] else None,
            })
        observed.sort(key=lambda it: SEVERITY_ORDER.get(it["severity"], 9))

        run_payload = {
            "id": run.id,
            "base_url": run.base_url,
            "started_at": _ensure_utc(run.started_at),
            "finished_at": _ensure_utc(run.finished_at),
            "duration_s": run.duration_s,
            "status": run.status,
            "health_score": run.health_score,
            "summary": run.summary or {},
        }

    return templates.TemplateResponse(
        request=request,
        name="history_detail.html",
        context={
            "active": "history",
            "base_url": base_url,
            "run": run_payload,
            "observed": observed,
            "nav_open_count": _open_count_for(base_url),
            "global_status": _global_status(base_url),
        },
    )


@app.get("/live", response_class=HTMLResponse)
async def live_page(request: Request):
    base_url = get_base_url()
    live = _live_snapshot(base_url)
    extras = [
        _live_snapshot(u) for u in get_live_targets()
        if u.rstrip("/") != base_url.rstrip("/")
    ]
    init_db()
    with get_session() as session:
        probes_count = session.scalar(
            select(sa_func.count(UptimeCheck.id))
            .where(UptimeCheck.target_url == base_url.rstrip("/"))
        ) or 0
    return templates.TemplateResponse(
        request=request,
        name="live.html",
        context={
            "active": "live",
            "base_url": base_url,
            "live": live,
            "live_extras": extras,
            "probes_count": probes_count,
            "nav_open_count": _open_count_for(base_url),
            "global_status": _global_status(base_url),
        },
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, target_url: str | None = None):
    init_db()
    base_url = get_base_url()
    selected = (target_url or base_url).rstrip("/")
    with get_session() as session:
        targets = sorted({t for t, in session.execute(
            select(Incident.target_url).distinct()
        ).all()})
        all_inc = session.scalar(
            select(sa_func.count(Incident.id)).where(Incident.target_url == selected)
        ) or 0
        open_inc = session.scalar(
            select(sa_func.count(Incident.id)).where(
                Incident.target_url == selected,
                Incident.resolved_at.is_(None),
            )
        ) or 0
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "active": "reports",
            "base_url": base_url,
            "selected_target": selected,
            "target_options": targets,
            "counts": {"all_incidents": all_inc, "open_incidents": open_inc},
            "nav_open_count": _open_count_for(base_url),
            "global_status": _global_status(base_url),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, changed: int = 0, error: str | None = None):
    init_db()
    import config as _cfg
    base_url = get_base_url()
    recent = get_recent_targets()
    target_changed_at = get_target_changed_at()
    with get_session() as session:
        runs_count = session.scalar(select(sa_func.count(Run.id))) or 0
        inc_count = session.scalar(select(sa_func.count(Incident.id))) or 0
        open_count = session.scalar(
            select(sa_func.count(Incident.id)).where(Incident.resolved_at.is_(None))
        ) or 0
        obs_count = session.scalar(select(sa_func.count(IncidentObservation.id))) or 0
        cats = sorted({c for c, in session.execute(
            select(Incident.category).distinct()
        ).all()})
        alerts_count = session.scalar(select(sa_func.count(Alert.id))) or 0
        recent_alerts = [
            {
                "kind": a.kind, "category": a.category, "severity": a.severity,
                "channel": a.channel, "dispatch_status": a.dispatch_status,
                "summary": a.summary, "fired_at": _ensure_utc(a.fired_at),
            }
            for a in session.scalars(
                select(Alert).order_by(desc(Alert.fired_at)).limit(12)
            ).all()
        ]

    db_url = os.environ.get("BT_MONITOR_DB_URL") or f"sqlite:///{(ROOT / 'data' / 'bt_monitor.db').as_posix()}"
    is_sqlite = db_url.startswith("sqlite")
    security = {
        "auth_enabled": _AUTH_ENABLED,
        "current_user": request.session.get("user"),
        "allowed_hosts": _cfg._allowed_hosts(),
        "allow_private": _cfg._allow_private_targets(),
        "log_format": os.environ.get("BT_MONITOR_LOG_FORMAT", "text"),
        "db_backend": "PostgreSQL" if not is_sqlite else "SQLite",
        "db_migrations": "create_all (dev)" if is_sqlite else "Alembic",
    }

    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "active": "settings",
            "base_url": base_url,
            "recent_targets": recent,
            "target_changed_at": target_changed_at,
            "changed_flag": changed == 1,
            "error_flag": error,
            "alert_severities": incident_alert_hub.severities,
            "alert_on_resolve": incident_alert_hub.alert_on_resolve,
            "teams_configured": bool(os.environ.get("BT_MONITOR_TEAMS_WEBHOOK")),
            "dashboard_url": os.environ.get("BT_MONITOR_DASHBOARD_URL"),
            "scan_interval_h": SCAN_INTERVAL_H,
            "pages_interval_s": PAGES_INTERVAL_S,
            "pages_watcher_count": _pages_watcher.page_count,
            "pages_watcher_summary": _pages_watcher.states_summary,
            "uptime_retention_days": UPTIME_RETENTION_DAYS,
            "db_url": db_url,
            "db_stats": {
                "runs": runs_count,
                "incidents": inc_count,
                "open": open_count,
                "observations": obs_count,
                "categories": cats,
                "alerts": alerts_count,
            },
            "security": security,
            "recent_alerts": recent_alerts,
            "nav_open_count": _open_count_for(base_url),
            "global_status": _global_status(base_url),
        },
    )


# ---------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------
def _filename(prefix: str, target_url: str | None, ext: str) -> str:
    from urllib.parse import urlparse
    host = "all"
    if target_url:
        host = urlparse(target_url).netloc.replace(":", "_") or "site"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"{prefix}-{host}-{ts}.{ext}"


@app.get("/export/incidents.csv")
async def export_csv(status: str = "all", target_url: str | None = None):
    data = build_incidents_csv(target_url, status)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{_filename("bt-monitor-incidents", target_url, "csv")}"'},
    )


@app.get("/export/bt-monitor.xlsx")
async def export_xlsx(target_url: str | None = None):
    data = build_incidents_xlsx(target_url)
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{_filename("bt-monitor", target_url, "xlsx")}"'},
    )


@app.get("/export/audit.pdf")
async def export_pdf(target_url: str | None = None):
    data = build_audit_pdf(target_url)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_filename("bt-monitor-audit", target_url, "pdf")}"'},
    )


# ---------------------------------------------------------------------
# Mutations: run / target / clear / live targets
# ---------------------------------------------------------------------
async def _run_subprocess():
    global _scan_in_flight
    log_path = DATA / "_run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "runner.py"),
            cwd=str(ROOT), env=env,
            stdout=log_file, stderr=asyncio.subprocess.STDOUT,
        )
        await proc.wait()
    finally:
        log_file.close()
        _scan_in_flight = False


async def _try_start_scan() -> tuple[bool, str]:
    """Start a deep scan if allowed. Returns (started, reason).

    Single-flight (one scan at a time) + cooldown (min seconds between starts),
    guarded by an asyncio lock so concurrent /run requests can't race past the
    check. reason ∈ {"started","already_running","cooldown"} (#5)."""
    global _scan_in_flight, _last_scan_start
    min_interval = _env_float("BT_MONITOR_SCAN_MIN_INTERVAL_S", 60.0)
    async with _scan_lock:
        if _scan_in_flight or read_state().get("status") == "running":
            return False, "already_running"
        now = asyncio.get_event_loop().time()
        if _last_scan_start and (now - _last_scan_start) < min_interval:
            return False, "cooldown"
        _scan_in_flight = True
        _last_scan_start = now
        asyncio.create_task(_run_subprocess())
        return True, "started"


@app.post("/run")
async def kick_run():
    started, reason = await _try_start_scan()
    if not started:
        return RedirectResponse(url=f"/?scan={reason}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/target")
async def change_target(url: str = Form(...), run_after: str | None = Form(None)):
    ok = set_base_url(url)
    if not ok:
        return RedirectResponse(url="/settings?error=invalid_url", status_code=303)
    _watcher.set_targets(all_watched_targets())
    if run_after:
        await _try_start_scan()
        return RedirectResponse(url="/", status_code=303)
    return RedirectResponse(url="/settings?changed=1", status_code=303)


@app.post("/clear")
async def clear_reports():
    for name in REPORT_FILES.values():
        p = DATA / name
        if p.exists():
            p.unlink()
    for extra in ("summary.json", "_run_state.json"):
        p = DATA / extra
        if p.exists():
            p.unlink()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/live/add")
async def live_add(url: str = Form(...)):
    add_live_target(url)
    _watcher.set_targets(all_watched_targets())
    return RedirectResponse(url="/live", status_code=303)


@app.post("/live/remove")
async def live_remove(url: str = Form(...)):
    remove_live_target(url)
    _watcher.set_targets(all_watched_targets())
    return RedirectResponse(url="/live", status_code=303)


# ---------------------------------------------------------------------
# JSON APIs
# ---------------------------------------------------------------------
@app.get("/api/state")
async def api_state():
    return read_state()


@app.get("/api/reports")
async def api_reports():
    return {
        "summary": _load_json("summary.json"),
        **{k: _load_json(v) for k, v in REPORT_FILES.items()},
    }


@app.get("/api/incidents")
async def api_incidents(
    status: str = "open",
    target_url: str | None = None,
    category: str | None = None,
    limit: int = 200,
):
    init_db()
    limit = max(1, min(limit, 1000))
    stmt = select(Incident)
    if target_url:
        stmt = stmt.where(Incident.target_url == target_url.rstrip("/"))
    if category:
        stmt = stmt.where(Incident.category == category)
    if status == "open":
        stmt = stmt.where(Incident.resolved_at.is_(None))
    elif status == "resolved":
        stmt = stmt.where(Incident.resolved_at.is_not(None))
    stmt = stmt.order_by(desc(Incident.last_seen_at)).limit(limit)
    with get_session() as session:
        items = [_serialize_incident(i) for i in session.scalars(stmt).all()]
        counts_stmt = (
            select(Incident.category, Incident.severity, sa_func.count(Incident.id))
            .where(Incident.resolved_at.is_(None))
        )
        if target_url:
            counts_stmt = counts_stmt.where(Incident.target_url == target_url.rstrip("/"))
        counts_stmt = counts_stmt.group_by(Incident.category, Incident.severity)
        agg: dict[str, dict[str, int]] = {}
        for cat, sev, cnt in session.execute(counts_stmt).all():
            agg.setdefault(cat, {})[sev] = cnt
    return {"items": items, "open_counts_by_category": agg, "count": len(items)}


@app.get("/api/incidents/{incident_id}")
async def api_incident_detail(incident_id: int):
    init_db()
    with get_session() as session:
        inc = session.get(Incident, incident_id)
        if inc is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        data = _serialize_incident(inc)
        data["details"] = inc.details
        data["observations"] = [
            {
                "run_id": o.run_id,
                "observed_at": _ensure_utc(o.observed_at).isoformat() if o.observed_at else None,
            }
            for o in inc.observations
        ]
    return data


@app.get("/api/runs")
async def api_runs(target_url: str | None = None, limit: int = 50):
    init_db()
    limit = max(1, min(limit, 500))
    stmt = select(Run).order_by(desc(Run.started_at)).limit(limit)
    if target_url:
        stmt = stmt.where(Run.base_url == target_url.rstrip("/"))
    with get_session() as session:
        runs = [_serialize_run(r) for r in session.scalars(stmt).all()]
    return {"items": runs, "count": len(runs)}


@app.get("/api/alerts")
async def api_alerts(limit: int = 50):
    limit = max(1, min(limit, 500))
    return {"items": incident_alert_hub.recent(limit=limit)}


@app.get("/api/uptime")
async def api_uptime():
    base = get_base_url()
    return {
        "primary": _live_snapshot(base),
        "extras": [
            _live_snapshot(u) for u in get_live_targets()
            if u.rstrip("/") != base.rstrip("/")
        ],
    }


if __name__ == "__main__":
    import uvicorn
    # #15 — host/port/log level configurable via env (default to localhost).
    uvicorn.run(
        app,
        host=os.environ.get("BT_MONITOR_HOST", "127.0.0.1"),
        port=int(os.environ.get("BT_MONITOR_PORT", "8000")),
        log_level=os.environ.get("BT_MONITOR_LOG_LEVEL", "info"),
    )
