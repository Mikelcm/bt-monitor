"""FastAPI dashboard for the BT monitor.

Endpoints:
    GET  /                  -> main dashboard HTML
    POST /run               -> kick off a full deep scan (runner.py subprocess)
    POST /target            -> change the target URL (persists + retargets watcher)
    POST /clear             -> wipe last run's reports
    GET  /api/state         -> deep-scan run state
    GET  /api/reports       -> JSON dump of every deep-scan report
    GET  /api/uptime        -> live watcher state (recent pings, uptime %, alerts)

A background watcher (monitoring.watcher.Watcher) is started in the FastAPI
lifespan and probes the configured target every ~30s, recording state and
firing alerts on state changes. When the target URL is changed via /target,
the watcher is retargeted live — no restart needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import (
    DATA_DIR, get_base_url, set_base_url, get_recent_targets,
    get_live_targets, add_live_target, remove_live_target, all_watched_targets,
)
from runner import read_state, build_summary
from monitoring.store import UptimeStore
from monitoring.alerts import AlertHub
from monitoring.watcher import Watcher

DATA = ROOT / DATA_DIR
DASH_DIR = ROOT / "dashboard"

# ---------------------------------------------------------------------
# global singletons (one per process)
# ---------------------------------------------------------------------
_store = UptimeStore()
_alerts = AlertHub()
_watcher = Watcher(store=_store, alerts=_alerts)
_watcher_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the watcher when the dashboard boots, stop on shutdown."""
    global _watcher_task
    _watcher.set_targets(all_watched_targets())
    _watcher_task = asyncio.create_task(_watcher.run_forever())
    try:
        yield
    finally:
        await _watcher.stop()
        if _watcher_task:
            try:
                await asyncio.wait_for(_watcher_task, timeout=5)
            except asyncio.TimeoutError:
                _watcher_task.cancel()


app = FastAPI(title="BT Monitor Dashboard", lifespan=lifespan)
templates = Jinja2Templates(directory=str(DASH_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(DASH_DIR / "static")), name="static")


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
    "content":       "content_report.json",
    "visual":        "visual_report.json",
}


# ---------------------------------------------------------------------
# pages
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    base_url = get_base_url()
    summary = _load_json("summary.json") or build_summary()
    state = read_state()
    reports = {k: _load_json(v) for k, v in REPORT_FILES.items()}
    recent = get_recent_targets()

    # critical issues for the alert banner
    criticals = []
    if summary.get("doc_leaks", 0) > 0 and reports.get("docs"):
        for d in reports["docs"].get("docs", []):
            if d.get("markers_found"):
                criticals.append({
                    "type": "doc_leak",
                    "title": "Internal document exposed publicly",
                    "url": d["url"],
                    "detail": ", ".join(m["marker"] for m in d["markers_found"][:3]),
                })

    # live watcher snapshots — one per watched target
    live = _live_snapshot(base_url)
    extras = [
        _live_snapshot(u) for u in get_live_targets() if u.rstrip("/") != base_url.rstrip("/")
    ]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "base_url": base_url,
            "summary": summary,
            "state": state,
            "reports": reports,
            "recent_targets": recent,
            "criticals": criticals,
            "live": live,
            "live_extras": extras,
            "has_data": bool(summary.get("pages")) and summary.get("base_url") == base_url,
            "stale_data": bool(summary.get("pages")) and summary.get("base_url") != base_url,
        },
    )


def _live_snapshot(base_url: str) -> dict:
    """Snapshot used to render the Live Status section + JSON endpoint."""
    target_state = _watcher.targets.get(base_url.rstrip("/"))
    pings = _store.recent_pings(base_url.rstrip("/"), limit=120)
    uptime_24h = _store.uptime_percent(base_url.rstrip("/"))
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
        "alerts": alerts,
    }


# ---------------------------------------------------------------------
# deep-scan run controls (existing)
# ---------------------------------------------------------------------
async def _run_subprocess():
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


@app.post("/run")
async def kick_run():
    state = read_state()
    if state.get("status") == "running":
        return JSONResponse({"ok": False, "reason": "already_running"}, status_code=409)
    asyncio.create_task(_run_subprocess())
    return RedirectResponse(url="/", status_code=303)


@app.post("/target")
async def change_target(url: str = Form(...), run_after: str | None = Form(None)):
    url = url.strip()
    if not url:
        return RedirectResponse(url="/", status_code=303)
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    set_base_url(url)
    # Retarget the live watcher: keep extras, swap primary.
    _watcher.set_targets(all_watched_targets())
    if run_after:
        state = read_state()
        if state.get("status") != "running":
            asyncio.create_task(_run_subprocess())
    return RedirectResponse(url="/", status_code=303)


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
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------
# JSON APIs
# ---------------------------------------------------------------------
@app.get("/report.html", response_class=HTMLResponse)
async def download_report(request: Request):
    """Self-contained HTML report — safe to save / email / archive."""
    base_url = get_base_url()
    summary = _load_json("summary.json") or build_summary()
    reports = {k: _load_json(v) for k, v in REPORT_FILES.items()}
    criticals = []
    if summary.get("doc_leaks", 0) > 0 and reports.get("docs"):
        for d in reports["docs"].get("docs", []):
            if d.get("markers_found"):
                criticals.append({
                    "type": "doc_leak",
                    "title": "Internal document exposed publicly",
                    "url": d["url"],
                    "detail": ", ".join(m["marker"] for m in d["markers_found"][:3]),
                })
    response = templates.TemplateResponse(
        request=request, name="report.html",
        context={"base_url": base_url, "summary": summary, "reports": reports, "criticals": criticals},
    )
    # Suggest a filename when the user clicks the link.
    from urllib.parse import urlparse
    host = urlparse(base_url).netloc.replace(":", "_") or "site"
    ts = (summary.get("generated_at") or "").replace(":", "-")[:19]
    response.headers["Content-Disposition"] = (
        f'attachment; filename="bt-monitor-{host}-{ts}.html"'
    )
    return response


@app.post("/live/add")
async def live_add(url: str = Form(...)):
    add_live_target(url)
    _watcher.set_targets(all_watched_targets())
    return RedirectResponse(url="/", status_code=303)


@app.post("/live/remove")
async def live_remove(url: str = Form(...)):
    remove_live_target(url)
    _watcher.set_targets(all_watched_targets())
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/state")
async def api_state():
    return read_state()


@app.get("/api/reports")
async def api_reports():
    return {
        "summary": _load_json("summary.json"),
        **{k: _load_json(v) for k, v in REPORT_FILES.items()},
    }


@app.get("/api/uptime")
async def api_uptime():
    base = get_base_url()
    return {
        "primary": _live_snapshot(base),
        "extras": [
            _live_snapshot(u) for u in get_live_targets() if u.rstrip("/") != base.rstrip("/")
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
