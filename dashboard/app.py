"""FastAPI dashboard for the BT monitor.

Endpoints:
    GET  /                  -> main dashboard HTML
    POST /run               -> kick off a full run (runner.py as subprocess)
    POST /target            -> change the target URL (persists to data/_target.json)
    POST /clear             -> wipe last run's reports
    GET  /api/state         -> current run state (idle / running / done)
    GET  /api/reports       -> JSON dump of every report

Each run launches `runner.py` as a fresh subprocess. That way changing the
target URL takes effect on the very next run — no service restart needed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
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
)
from runner import read_state, build_summary

DATA = ROOT / DATA_DIR
DASH_DIR = ROOT / "dashboard"

app = FastAPI(title="BT Monitor Dashboard")
templates = Jinja2Templates(directory=str(DASH_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(DASH_DIR / "static")), name="static")

_run_proc: asyncio.subprocess.Process | None = None
_run_lock = asyncio.Lock()


def _load_json(name: str) -> dict | None:
    p = DATA / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


REPORT_FILES = {
    "sitemap": "sitemap_pages.json",
    "links": "links_report.json",
    "performance": "performance_report.json",
    "assets": "assets_report.json",
    "forms": "forms_report.json",
    "docs": "docs_report.json",
    "visual": "visual_report.json",
}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    base_url = get_base_url()
    summary = _load_json("summary.json") or build_summary()
    state = read_state()
    reports = {k: _load_json(v) for k, v in REPORT_FILES.items()}
    recent = get_recent_targets()
    # Top-level critical issues for the alert banner.
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
            "has_data": bool(summary.get("pages")) and summary.get("base_url") == base_url,
            "stale_data": bool(summary.get("pages")) and summary.get("base_url") != base_url,
        },
    )


async def _run_subprocess():
    """Launch runner.py as a fresh subprocess, stream output to data/_run.log."""
    global _run_proc
    log_path = DATA / "_run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    # PYTHONIOENCODING ensures the child writes UTF-8 to its stdout/stderr.
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        _run_proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "runner.py"),
            cwd=str(ROOT),
            env=env,
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
        )
        await _run_proc.wait()
    finally:
        log_file.close()
        _run_proc = None


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
    # Light normalization — let users paste "example.com" without scheme.
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    set_base_url(url)
    # Optional: kick off a run right after switching.
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


@app.get("/api/state")
async def api_state():
    return read_state()


@app.get("/api/reports")
async def api_reports():
    return {
        "summary": _load_json("summary.json"),
        **{k: _load_json(v) for k, v in REPORT_FILES.items()},
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
