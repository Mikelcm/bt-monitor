"""Orchestrator: run every check in order and produce a unified summary.

Sequence:
  1. crawler/sitemap.py  -> data/sitemap_pages.json
  2. checks/links.py     -> data/links_report.json
  3. checks/performance  -> data/performance_report.json
  4. checks/assets       -> data/assets_report.json
  5. checks/forms        -> data/forms_report.json
  6. checks/docs         -> data/docs_report.json
  -> data/summary.json (top-level aggregate, used by the dashboard)

Run:
    python runner.py
    BT_MONITOR_BASE_URL=http://127.0.0.1:8765 python runner.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from config import BASE_URL, DATA_DIR
from crawler.sitemap import main as run_crawler
from checks.links import main as run_links
from checks.performance import main as run_performance
from checks.assets import main as run_assets
from checks.forms import main as run_forms
from checks.docs import main as run_docs
from checks.accessibility import main as run_accessibility
from checks.seo import main as run_seo
from checks.console_errors import main as run_console
from db.persist import persist_run
from monitoring.incident_alerts import hub as alert_hub

DATA = ROOT / DATA_DIR
STATE_FILE = DATA / "_run_state.json"


def write_state(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"status": "idle", "current_step": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle", "current_step": None}


STEPS = [
    ("crawler",       "Discover pages from sitemap",          run_crawler),
    ("links",         "Check for broken links",               run_links),
    ("performance",   "Measure page load performance",        run_performance),
    ("assets",        "Detect missing images/fonts",          run_assets),
    ("forms",         "Detect form issues",                   run_forms),
    ("docs",          "Scan documents for 'Uz intern'",       run_docs),
    ("accessibility", "WCAG 2 AA accessibility (axe-core)",   run_accessibility),
    ("seo",           "SEO health (meta, headings, canonical)", run_seo),
    ("console",       "JS errors + failed requests",          run_console),
]


async def run_all() -> dict:
    started = datetime.now(timezone.utc).isoformat()
    t0 = time.perf_counter()
    state = {
        "status": "running",
        "started_at": started,
        "base_url": BASE_URL,
        "current_step": None,
        "completed_steps": [],
        "errors": [],
    }
    write_state(state)

    for slug, description, func in STEPS:
        print(f"\n{'='*70}\n>>> {description}\n{'='*70}", flush=True)
        state["current_step"] = slug
        write_state(state)
        step_t0 = time.perf_counter()
        try:
            await func()
            state["completed_steps"].append({
                "step": slug,
                "duration_s": round(time.perf_counter() - step_t0, 2),
            })
        except Exception as exc:
            tb = traceback.format_exc()
            print(f"!! step {slug} failed: {exc}\n{tb}", flush=True)
            state["errors"].append({"step": slug, "error": repr(exc), "traceback": tb})
        write_state(state)

    state["status"] = "done"
    state["current_step"] = None
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    state["total_duration_s"] = round(time.perf_counter() - t0, 2)
    write_state(state)

    summary = build_summary()
    (DATA / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    try:
        persist_stats = persist_run(BASE_URL, summary, state)
        print(
            f"[db] run #{persist_stats['run_id']} persisted — "
            f"new={persist_stats['new']} still_open={persist_stats['still_open']} "
            f"reopened={persist_stats['reopened']} resolved={persist_stats['resolved']}",
            flush=True,
        )
        events = persist_stats.get("events") or []
        if events:
            alert_stats = alert_hub.fire_batch(events)
            print(
                f"[alerts] {alert_stats['recorded']} event(s) — "
                f"dispatched={alert_stats['dispatched']} skipped={alert_stats['skipped']} "
                f"channel_errors={alert_stats['channel_errors']}",
                flush=True,
            )
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"!! db persist failed: {exc}\n{tb}", flush=True)
        state.setdefault("errors", []).append({"step": "db_persist", "error": repr(exc), "traceback": tb})
        write_state(state)

    print(f"\n{'='*70}")
    print(f"DONE in {state['total_duration_s']}s — target {BASE_URL}")
    print(f"Pages: {summary['pages']}  Broken links: {summary['broken_links']}  "
          f"Slow pages: {summary['slow_pages']}  Asset fails: {summary['asset_failures']}  "
          f"Form issues: {summary['form_issues']}  Doc leaks: {summary['doc_leaks']}")
    print(f"  A11y: {summary['a11y_critical']} crit / {summary['a11y_serious']} serious  "
          f"SEO: {summary['seo_issues']}  "
          f"JS errors: {summary['console_js_errors']}  "
          f"Failed reqs: {summary['console_failed_requests']}")
    print(f"Summary: {DATA / 'summary.json'}")
    return summary


def build_summary() -> dict:
    """Build a top-level aggregate from all the per-check report JSON files."""
    def load(name: str) -> dict:
        p = DATA / name
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    sitemap = load("sitemap_pages.json")
    links = load("links_report.json")
    perf = load("performance_report.json")
    assets = load("assets_report.json")
    forms = load("forms_report.json")
    docs = load("docs_report.json")
    a11y = load("accessibility_report.json")
    seo = load("seo_report.json")
    console = load("console_report.json")

    # How was the page set discovered? Used by the dashboard to surface when
    # the scan was effectively blocked (only the homepage got through).
    discovery_method = sitemap.get("discovery_method", "unknown")
    sitemap_errors = sitemap.get("errors", []) or []
    blocked_403_count = sum(
        1 for e in sitemap_errors
        if (e.get("debug") or {}).get("status") == 403 or e.get("status") == 403
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": BASE_URL,
        "pages": sitemap.get("page_count", 0),
        "discovery_method": discovery_method,
        "blocked_403_count": blocked_403_count,
        "unique_links": links.get("unique_links", 0),
        "broken_links": links.get("broken_count", 0),
        "redirects": links.get("redirect_count", 0),
        "slow_pages": perf.get("slow_count", 0),
        "perf_threshold_ms": perf.get("threshold_ms"),
        "asset_failures": assets.get("total_failed_assets", 0),
        "asset_failures_by_type": assets.get("failed_by_type", {}),
        "total_forms": forms.get("total_forms", 0),
        "form_issues": forms.get("forms_with_issues", 0),
        "docs_scanned": docs.get("docs_found", 0),
        "doc_leaks": docs.get("leaks_found", 0),
        "a11y_critical": a11y.get("total_critical", 0),
        "a11y_serious":  a11y.get("total_serious", 0),
        "a11y_pages_with_issues": a11y.get("pages_with_issues", 0),
        "seo_issues":   seo.get("total_issues", 0),
        "seo_pages_with_issues": seo.get("pages_with_issues", 0),
        "console_js_errors":      console.get("total_js_errors", 0),
        "console_errors":         console.get("total_console_errors", 0),
        "console_failed_requests": console.get("total_failed_requests", 0),
    }
    # Overall health score: 100 minus weighted penalties.
    penalty = (
        summary["broken_links"] * 3
        + summary["slow_pages"] * 4
        + summary["asset_failures"] * 2
        + summary["form_issues"] * 5
        + summary["doc_leaks"] * 20
        + summary["a11y_critical"] * 4
        + summary["a11y_serious"] * 2
        + summary["seo_issues"] * 1
        + summary["console_js_errors"] * 2
        + summary["console_failed_requests"] * 1
    )
    summary["health_score"] = max(0, 100 - penalty)
    # If we didn't discover any pages, the scan effectively failed — don't
    # pretend everything is healthy.
    if summary["pages"] == 0:
        summary["status"] = "no_data"
        summary["health_score"] = None
    elif discovery_method == "homepage_only" or blocked_403_count > 0:
        # Scan was crippled by a WAF / bot manager. The "0 issues" we see
        # come from the fact that we couldn't actually read the site, not
        # from the site being healthy. Tell the truth.
        summary["status"] = "blocked"
        summary["health_score"] = None
    else:
        summary["status"] = (
            "critical" if summary["doc_leaks"] > 0 or penalty >= 30
            else "warn" if penalty > 0
            else "healthy"
        )
    return summary


if __name__ == "__main__":
    asyncio.run(run_all())
