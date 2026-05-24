"""Central config + runtime-overridable target URL.

Resolution priority for the target URL (highest wins):
  1. `data/_target.json`  — written by the dashboard "Change target" form.
  2. `BT_MONITOR_BASE_URL` environment variable.
  3. The default in DEFAULT_BASE_URL below.

`BASE_URL` is kept as a module-level constant for back-compat — modules that
import it at startup will see whatever was resolved at import time.
For LIVE override (dashboard subprocess), the dashboard launches `runner.py`
as a fresh subprocess so config.py re-evaluates with the new override file.
"""
import json
import os
from pathlib import Path

# Default if nothing else is set.
DEFAULT_BASE_URL = "https://www.resetmedia.ro"

DATA_DIR = "data"
_HERE = Path(__file__).resolve().parent
_OVERRIDE_FILE = _HERE / DATA_DIR / "_target.json"
_RECENT_FILE = _HERE / DATA_DIR / "_recent_targets.json"
_LIVE_TARGETS_FILE = _HERE / DATA_DIR / "_live_targets.json"


def get_base_url() -> str:
    """Resolve the target URL at call time (override > env > default)."""
    if _OVERRIDE_FILE.exists():
        try:
            data = json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
            url = (data or {}).get("base_url")
            if url:
                return url.rstrip("/")
        except Exception:
            pass
    env = os.environ.get("BT_MONITOR_BASE_URL")
    if env:
        return env.rstrip("/")
    return DEFAULT_BASE_URL.rstrip("/")


def set_base_url(url: str) -> None:
    """Persist a new target URL and add it to the recent list."""
    url = url.strip().rstrip("/")
    if not url:
        return
    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OVERRIDE_FILE.write_text(json.dumps({"base_url": url}), encoding="utf-8")
    _push_recent(url)


def get_recent_targets(limit: int = 6) -> list[str]:
    if not _RECENT_FILE.exists():
        return []
    try:
        items = json.loads(_RECENT_FILE.read_text(encoding="utf-8"))
        return list(items)[:limit]
    except Exception:
        return []


def _push_recent(url: str) -> None:
    existing = get_recent_targets(limit=20)
    existing = [u for u in existing if u != url]
    existing.insert(0, url)
    existing = existing[:20]
    _RECENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _RECENT_FILE.write_text(json.dumps(existing), encoding="utf-8")


def get_live_targets() -> list[str]:
    """Extra URLs the live watcher should monitor in parallel with the active deep-scan target."""
    if not _LIVE_TARGETS_FILE.exists():
        return []
    try:
        return [str(u).rstrip("/") for u in json.loads(_LIVE_TARGETS_FILE.read_text(encoding="utf-8"))]
    except Exception:
        return []


def _normalize_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def add_live_target(url: str) -> list[str]:
    url = _normalize_url(url)
    if not url:
        return get_live_targets()
    existing = get_live_targets()
    if url not in existing:
        existing.append(url)
        _LIVE_TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LIVE_TARGETS_FILE.write_text(json.dumps(existing), encoding="utf-8")
    return existing


def remove_live_target(url: str) -> list[str]:
    url = _normalize_url(url)
    existing = [u for u in get_live_targets() if u != url]
    _LIVE_TARGETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LIVE_TARGETS_FILE.write_text(json.dumps(existing), encoding="utf-8")
    return existing


def all_watched_targets() -> list[str]:
    """The full list the watcher should ping: the active deep-scan target + every extra live target."""
    primary = get_base_url()
    extras = get_live_targets()
    out: list[str] = []
    for u in [primary] + extras:
        u = u.rstrip("/")
        if u and u not in out:
            out.append(u)
    return out


# Resolved values at import time.
BASE_URL = get_base_url()
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

CRAWL_TIMEOUT_MS = 30_000
SLOW_PAGE_THRESHOLD_MS = 3_000
