"""Pytest config — must run BEFORE any project module is imported, so we set
env vars here at module top level (pytest imports conftest first).

Every test runs against an isolated temp SQLite DB and known auth creds.
"""
import os
import tempfile
from pathlib import Path

# Isolated DB per test session (never touch the dev/prod DB).
_TMP_DB = Path(tempfile.gettempdir()) / "bt_monitor_pytest.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(_TMP_DB) + suffix)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
os.environ["BT_MONITOR_DB_URL"] = f"sqlite:///{_TMP_DB.as_posix()}"

# Known auth creds so the security tests can assert 401/200.
os.environ["BT_MONITOR_AUTH_USER"] = "testuser"
os.environ["BT_MONITOR_AUTH_PASS"] = "testpass"

# SSRF strict by default (tests opt-in explicitly where needed).
os.environ.pop("BT_MONITOR_ALLOW_PRIVATE_TARGETS", None)
os.environ.pop("BT_MONITOR_ALLOWED_HOSTS", None)

# Disable the auto-scan scheduler during tests.
os.environ["BT_MONITOR_SCAN_INTERVAL_H"] = "0"

import pytest  # noqa: E402
from db.models import init_db, reset_schema_ready  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_db():
    reset_schema_ready()
    init_db()
    yield
