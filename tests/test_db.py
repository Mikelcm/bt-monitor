"""DB hardening tests: SQLite WAL (#7), init_db idempotency (#10),
Alert table (#8)."""
from sqlalchemy import text, select, func

from db import models
from db.models import get_engine, get_session, init_db, Alert


def test_wal_enabled():
    init_db()
    with get_engine().connect() as c:
        assert str(c.execute(text("PRAGMA journal_mode")).scalar()).lower() == "wal"
        assert int(c.execute(text("PRAGMA busy_timeout")).scalar()) >= 5000


def test_init_db_idempotent():
    init_db()
    assert models._schema_ready is True
    init_db()  # second call is a cheap no-op, must not raise
    assert models._schema_ready is True


def test_alert_table_roundtrip():
    init_db()
    with get_session() as s:
        before = s.scalar(select(func.count(Alert.id))) or 0
        s.add(Alert(kind="new", category="site_down", severity="critical",
                    channel="log", target_url="https://x", page_url="https://x",
                    summary="t", dispatched=True, dispatch_status="sent"))
        s.commit()
        after = s.scalar(select(func.count(Alert.id))) or 0
    assert after == before + 1
