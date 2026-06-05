"""Performance regression detector: baseline vs recent latency."""
from datetime import datetime, timedelta, timezone

from db.models import UptimeCheck, get_session, init_db
from monitoring import perf_regression as perf


def _seed(target, *, base_ms, base_n, recent_ms, recent_n, state="up"):
    init_db()
    now = datetime.now(timezone.utc)
    with get_session() as s:
        # baseline window: between -7d and -2h (use -1d)
        for i in range(base_n):
            s.add(UptimeCheck(target_url=target, checked_at=now - timedelta(days=1, minutes=i),
                              state=state, status_code=200, response_ms=base_ms, ok=True))
        # recent window: within the last hour (use -20min)
        for i in range(recent_n):
            s.add(UptimeCheck(target_url=target, checked_at=now - timedelta(minutes=20, seconds=i),
                              state=state, status_code=200, response_ms=recent_ms, ok=True))
        s.commit()
    return now


def test_regression_detected():
    t = "https://perf-regress.example"
    now = _seed(t, base_ms=600, base_n=40, recent_ms=1400, recent_n=10)
    with get_session() as s:
        v = perf.evaluate(s, t, now)
    assert v["enough_data"] is True
    assert v["baseline_ms"] == 600
    assert v["recent_ms"] == 1400
    assert v["regressed"] is True
    assert v["pct"] > 100


def test_no_regression_when_stable():
    t = "https://perf-stable.example"
    now = _seed(t, base_ms=600, base_n=40, recent_ms=680, recent_n=10)
    with get_session() as s:
        v = perf.evaluate(s, t, now)
    assert v["enough_data"] is True
    assert v["regressed"] is False


def test_not_enough_baseline_samples():
    t = "https://perf-sparse.example"
    now = _seed(t, base_ms=600, base_n=5, recent_ms=1400, recent_n=10)
    with get_session() as s:
        v = perf.evaluate(s, t, now)
    assert v["enough_data"] is False
    assert v["regressed"] is False


def test_fast_pages_ignored():
    # tiny absolute latencies (under MIN_MS) shouldn't trip the percentage check
    t = "https://perf-fast.example"
    now = _seed(t, base_ms=40, base_n=40, recent_ms=120, recent_n=10)
    with get_session() as s:
        v = perf.evaluate(s, t, now)
    assert v["enough_data"] is False   # baseline below MIN_MS floor


def test_check_opens_and_resolves_incident():
    from db.models import Incident
    from sqlalchemy import select
    t = "https://perf-incident.example"
    _seed(t, base_ms=500, base_n=40, recent_ms=1500, recent_n=10)
    stats = perf.check_perf_regressions()
    assert stats["opened"] >= 1
    with get_session() as s:
        inc = s.scalar(select(Incident).where(
            Incident.target_url == t, Incident.category == "perf_regression"))
        assert inc is not None and inc.resolved_at is None
