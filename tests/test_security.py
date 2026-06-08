"""Security regression tests: SSRF (#2), CSV/formula injection (#3),
Basic Auth (#1), CSRF same-origin (#4)."""
import base64

import pytest
from fastapi.testclient import TestClient

import config
from dashboard import exports
from dashboard.app import app


# ---------------- #2 SSRF ----------------
@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data",   # cloud metadata
    "http://localhost:8000",
    "http://127.0.0.1:8765",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
])
def test_ssrf_blocks_internal(url):
    assert config._validate_url(url) is None


def test_ssrf_allows_public():
    assert config._validate_url("https://www.resetmedia.ro") == "https://www.resetmedia.ro"


def test_ssrf_allowlist(monkeypatch):
    monkeypatch.setenv("BT_MONITOR_ALLOWED_HOSTS", "bancatransilvania.ro")
    assert config._validate_url("https://www.bancatransilvania.ro") == "https://www.bancatransilvania.ro"
    assert config._validate_url("https://evil.com") is None


def test_ssrf_private_optin(monkeypatch):
    monkeypatch.setenv("BT_MONITOR_ALLOW_PRIVATE_TARGETS", "true")
    assert config._validate_url("http://127.0.0.1:8765") == "http://127.0.0.1:8765"


# ---------------- #3 CSV/formula injection ----------------
@pytest.mark.parametrize("raw,expected", [
    ("=1+1", "'=1+1"),
    ("+x", "'+x"),
    ("-x", "'-x"),
    ("@x", "'@x"),
    ("Banca", "Banca"),
])
def test_formula_safe(raw, expected):
    assert exports._formula_safe(raw) == expected


def test_h_strips_and_escapes():
    assert exports._h("=șabloî") == "'=sabloi"


# ---------------- #1 Basic Auth + #4 CSRF ----------------
client = TestClient(app)   # no context manager → lifespan/watchers do not start


def _auth(user="testuser", pw="testpass"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_healthz_open():
    assert client.get("/healthz").status_code == 200


def test_root_redirects_to_login():
    # Browser navigation with no credentials is redirected to the login page
    # (session-based human auth), not shown the raw Basic Auth popup.
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_api_requires_auth_401():
    # API/automation paths still return 401 (no redirect) when unauthenticated.
    assert client.get("/api/state", follow_redirects=False).status_code == 401


def test_root_with_auth():
    assert client.get("/", headers=_auth()).status_code == 200


def test_root_wrong_pass():
    assert client.get("/", headers=_auth(pw="nope")).status_code == 401


def test_login_page_open():
    assert client.get("/login").status_code == 200


def test_login_success_sets_session():
    fresh = TestClient(app)
    r = fresh.post("/login", data={"username": "testuser", "password": "testpass"},
                   headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303
    # session cookie now lets us reach a protected page without Basic auth
    assert fresh.get("/").status_code == 200


def test_login_bad_credentials():
    fresh = TestClient(app)
    r = fresh.post("/login", data={"username": "testuser", "password": "wrong"},
                   headers={"Origin": "http://testserver"})
    assert r.status_code == 401


def test_csrf_foreign_origin_blocked():
    r = client.post("/live/remove",
                    headers={**_auth(), "Origin": "http://evil.com"},
                    data={"url": "http://x"})
    assert r.status_code == 403


def test_csrf_same_origin_ok():
    r = client.post("/live/remove",
                    headers={**_auth(), "Origin": "http://testserver"},
                    data={"url": "http://x"}, follow_redirects=False)
    assert r.status_code in (302, 303)
