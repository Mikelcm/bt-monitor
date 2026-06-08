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
import ipaddress
import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger("config")


# ---------------------------------------------------------------------
# .env auto-loader (so `uvicorn ...` picks up a local .env with no extra
# tooling). Real environment variables always win — .env only fills gaps.
# This runs at import time, before any os.environ reads below.
# ---------------------------------------------------------------------
def _load_dotenv() -> None:
    # Tests set this so the suite runs against a controlled env, not a dev .env.
    if os.environ.get("BT_MONITOR_SKIP_DOTENV"):
        return
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, val)   # don't override real env
    except Exception:
        pass


_load_dotenv()

# ---------------------------------------------------------------------
# SSRF protection (#2)
# ---------------------------------------------------------------------
# A target URL is attacker-influenced (set via the dashboard). Without a host
# check, someone could point the scanner at cloud metadata (169.254.169.254),
# localhost admin panels, or internal RFC-1918 hosts — Server-Side Request
# Forgery. We resolve the host and reject private / loopback / link-local /
# reserved IPs by default.
#
# Two env knobs:
#   BT_MONITOR_ALLOWED_HOSTS         comma list; if set, ONLY these hosts (and
#                                    their subdomains) are allowed. Production
#                                    should set this to the BT domains.
#   BT_MONITOR_ALLOW_PRIVATE_TARGETS truthy → permit loopback/private targets,
#                                    needed for the local test fixture
#                                    (http://127.0.0.1:8765). Off by default.

def _allowed_hosts() -> list[str]:
    raw = os.environ.get("BT_MONITOR_ALLOWED_HOSTS", "")
    return [h.strip().lower().lstrip(".") for h in raw.split(",") if h.strip()]


def _allow_private_targets() -> bool:
    return os.environ.get("BT_MONITOR_ALLOW_PRIVATE_TARGETS", "").lower() in ("1", "true", "yes")


def _host_block_reason(host: str) -> str | None:
    """Return a reason string if `host` must be blocked, else None."""
    if not host:
        return "empty_host"
    host_l = host.lower().strip("[]")  # strip IPv6 brackets

    # Allowlist (production): host must match an allowed domain or subdomain.
    allow = _allowed_hosts()
    if allow:
        ok = any(host_l == d or host_l.endswith("." + d) for d in allow)
        if not ok:
            return f"host_not_in_allowlist ({host_l})"

    # Resolve and inspect every resulting IP. Block internal ranges.
    if _allow_private_targets():
        return None  # explicit dev opt-in (fixture)
    try:
        infos = socket.getaddrinfo(host_l, None)
    except Exception:
        # Can't resolve — let the probe fail naturally rather than block a
        # transient DNS hiccup. The IP-class check below is the real guard.
        return None
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return f"resolves_to_internal_ip ({ip_str})"
    return None

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


def _human_reason(reason: str, host: str) -> str:
    """Translate an SSRF block reason into a clear Romanian message."""
    if reason.startswith("host_not_in_allowlist"):
        allow = ", ".join(_allowed_hosts())
        return (f"Host-ul „{host}” nu e în lista permisă. "
                f"Permise acum: {allow}. Modifică BT_MONITOR_ALLOWED_HOSTS sau "
                f"golește variabila ca să permiți orice site.")
    if reason.startswith("resolves_to_internal_ip"):
        return (f"Ținta „{host}” indică un IP intern/privat — blocată de protecția "
                f"SSRF. Pentru ținte locale (ex. fixture pe 127.0.0.1) pornește cu "
                f"BT_MONITOR_ALLOW_PRIVATE_TARGETS=true.")
    if reason == "empty_host":
        return "URL fără host valid."
    return f"Țintă respinsă de protecția SSRF ({reason})."


def validate_target(url: str) -> tuple[str | None, str]:
    """Return (clean_url, '') on success, or (None, human_reason) on rejection.
    Adds https:// if missing; enforces the SSRF host policy (#2)."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return None, "URL gol."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        p = urlparse(url)
        if not p.netloc:
            return None, "URL fără host valid."
        host = p.hostname or ""
    except Exception:
        return None, "URL invalid."
    reason = _host_block_reason(host)
    if reason:
        log.warning("target URL rejected (SSRF guard): %s — %s", url, reason)
        return None, _human_reason(reason, host)
    return url, ""


def _validate_url(url: str) -> str | None:
    """Back-compat helper (used by tests + live-target gate). URL or None."""
    clean, _ = validate_target(url)
    return clean


def set_base_url(url: str) -> tuple[bool, str]:
    """Persist a new target URL. Returns (ok, message).

    Atomic write (write to temp file then rename) to avoid corruption if the
    process dies mid-write. Stamps changed_at so the UI can show 'last changed'.
    """
    clean, reason = validate_target(url)
    if clean is None:
        log.warning("set_base_url rejected URL %r: %s", url, reason)
        return False, reason
    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": clean,
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = _OVERRIDE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_OVERRIDE_FILE)
    _push_recent(clean)
    log.info("base_url -> %s", clean)
    return True, ""


def get_target_changed_at() -> str | None:
    if not _OVERRIDE_FILE.exists():
        return None
    try:
        data = json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8"))
        return data.get("changed_at")
    except Exception:
        return None


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
    # SSRF-gate live targets too (#2) — same guard as the primary target.
    url = _validate_url(url)
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

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Per-page navigation timeout. 20s is plenty for a healthy page; lower than the
# old 30s so one stuck page can't stall the whole scan for half a minute.
CRAWL_TIMEOUT_MS = _env_int("BT_MONITOR_CRAWL_TIMEOUT_MS", 20_000)
SLOW_PAGE_THRESHOLD_MS = _env_int("BT_MONITOR_SLOW_PAGE_THRESHOLD_MS", 3_000)
# Hard cap on how many discovered pages the deep scan will process. Each page is
# loaded in a browser by ~7 checks, so an unbounded sitemap (hundreds of pages)
# turns a scan into an hour-long job. 40 keeps a full scan to a few minutes.
MAX_SCAN_PAGES = _env_int("BT_MONITOR_MAX_SCAN_PAGES", 40)
