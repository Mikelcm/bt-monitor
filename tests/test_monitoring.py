"""Monitoring tests: classify/hysteresis (watcher), DB consolidation (#6),
incident alert dispatch filter + persistence + payloads (#8/#11)."""
from sqlalchemy import select, func

from monitoring.watcher import classify
from monitoring import uptime_persist
from monitoring.incident_alerts import IncidentAlert, hub
from db.models import get_session, Alert, init_db


# ---------------- watcher.classify ----------------
def test_classify_up():
    assert classify(200, 100, None) == "up"

def test_classify_slow():
    assert classify(200, 99999, None) == "slow"

def test_classify_down_status():
    assert classify(500, 100, None) == "down"

def test_classify_down_error():
    assert classify(None, None, "conn refused") == "down"


# ---------------- #6 single source of truth ----------------
def test_store_module_removed():
    import pytest
    with pytest.raises(ModuleNotFoundError):
        import monitoring.store  # noqa


def test_recent_pings_and_uptime():
    init_db()
    t = "https://montest.example"
    for st, code, ms, err in [("up", 200, 120, None), ("up", 200, 130, None),
                              ("down", None, None, "x")]:
        uptime_persist.record_probe(t, st, code, ms, err)
    rp = uptime_persist.recent_pings(t, limit=10)
    assert len(rp) >= 3
    assert rp[-1]["state"] == "down"            # newest last
    assert {"ts", "state", "response_ms", "status", "error"} <= set(rp[0])
    pct = uptime_persist.uptime_percent(t, hours=24)
    assert pct is not None and 0 <= pct <= 100


# ---------------- #8 / #11 incident alerts ----------------
def _alert(severity="critical", kind="new"):
    return IncidentAlert(kind=kind, incident_id=None, fingerprint="fp|x",
                         category="site_down", severity=severity,
                         summary="t", target_url="https://x", page_url="https://x", run_id=0)


def test_should_dispatch_filter():
    assert hub.should_dispatch(_alert("critical")) is True
    assert hub.should_dispatch(_alert("minor")) is False   # below default threshold


def test_alert_persisted_to_db():
    init_db()
    with get_session() as s:
        before = s.scalar(select(func.count(Alert.id))) or 0
    hub.fire_batch([_alert("critical")])
    with get_session() as s:
        after = s.scalar(select(func.count(Alert.id))) or 0
    assert after == before + 1


def test_payload_shapes():
    a = _alert()
    mc = hub._messagecard_payload(a)
    ac = hub._adaptive_payload(a)
    assert mc["@type"] == "MessageCard"
    assert ac["type"] == "message"
    assert ac["attachments"][0]["content"]["type"] == "AdaptiveCard"
