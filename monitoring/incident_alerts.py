"""Incident-based alert dispatcher for deep-scan findings.

Counterpart to monitoring.alerts.AlertHub — that one fires on live-watcher
state changes (every ~30s). This one fires only on the lifecycle edges of
a deep-scan incident:

  * new       — first time we ever see this fingerprint
  * reopened  — fingerprint was previously resolved, now back
  * resolved  — fingerprint was open, did not show up in latest scan

Because incidents are deduped by (target_url, fingerprint), a persistent
issue triggers ONE "new" alert across all subsequent scans — no flood.

Configuration (env vars, all optional):
  BT_MONITOR_TEAMS_WEBHOOK     — POST cards here
  BT_MONITOR_ALERT_SEVERITIES  — CSV, default "critical,serious"
  BT_MONITOR_ALERT_ON_RESOLVE  — "true"/"false", default "true"
  BT_MONITOR_DASHBOARD_URL     — used to build links in messages
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import httpx

_HERE = Path(__file__).resolve().parent.parent
_ALERTS_LOG = _HERE / "data" / "_incident_alerts.jsonl"

log = logging.getLogger("monitoring.incident_alerts")


_KIND_LABELS = {
    "new":      "NEW",
    "reopened": "REOPENED",
    "resolved": "RESOLVED",
}


@dataclass
class IncidentAlert:
    kind: str            # "new" | "reopened" | "resolved"
    incident_id: int
    fingerprint: str
    category: str
    severity: str        # "critical" | "serious" | "moderate" | "minor"
    summary: str
    target_url: str
    page_url: str | None
    run_id: int
    fired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def title(self) -> str:
        return f"[{_KIND_LABELS.get(self.kind, self.kind)}] {self.category} ({self.severity})"

    @property
    def color_hex(self) -> str:
        """Teams card themeColor — without leading #."""
        if self.kind == "resolved":
            return "10b981"
        return {
            "critical": "b91c1c",
            "serious":  "ef4444",
            "moderate": "f59e0b",
            "minor":    "64748b",
        }.get(self.severity, "64748b")

    def to_dict(self) -> dict:
        return {
            "kind":          self.kind,
            "incident_id":   self.incident_id,
            "fingerprint":   self.fingerprint,
            "category":      self.category,
            "severity":      self.severity,
            "summary":       self.summary,
            "target_url":    self.target_url,
            "page_url":      self.page_url,
            "run_id":        self.run_id,
            "fired_at":      self.fired_at.isoformat(),
        }


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, default)
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class IncidentAlertHub:
    """In-memory ring buffer + outbound webhook dispatch for incident events.

    Singleton-ish: create once in the dashboard process, also instantiable
    from runner.py for batch dispatch. Both share configuration via env vars,
    not state — that's fine because the alert ring is a viewer convenience,
    not a source of truth (the DB is).
    """

    def __init__(self, ring_size: int = 200):
        self._ring_size = ring_size
        self.severities = _csv_env("BT_MONITOR_ALERT_SEVERITIES", "critical,serious")
        self.alert_on_resolve = _bool_env("BT_MONITOR_ALERT_ON_RESOLVE", True)
        self._log_path = _ALERTS_LOG

    # ------------------------------------------------------------------
    # filtering
    # ------------------------------------------------------------------
    def should_dispatch(self, alert: IncidentAlert) -> bool:
        if alert.kind == "resolved" and not self.alert_on_resolve:
            return False
        return alert.severity.lower() in self.severities

    # ------------------------------------------------------------------
    # firing
    # ------------------------------------------------------------------
    def fire_batch(self, alerts: Iterable[IncidentAlert]) -> dict:
        """Record every alert in the ring buffer; dispatch the ones that pass
        the severity filter. Returns a small stats dict."""
        stats = {"recorded": 0, "dispatched": 0, "skipped": 0, "channel_errors": 0}
        teams_url = os.environ.get("BT_MONITOR_TEAMS_WEBHOOK")

        # Open a single httpx client for the batch.
        client = httpx.Client(timeout=10) if teams_url else None
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        log_lines: list[str] = []
        try:
            for a in alerts:
                stats["recorded"] += 1
                dispatched_to: list[str] = []
                if self.should_dispatch(a):
                    log.warning("INCIDENT ALERT %s :: %s", a.title, a.summary)
                    if teams_url:
                        if self._send_teams(client, teams_url, a):
                            dispatched_to.append("teams")
                        else:
                            stats["channel_errors"] += 1
                    stats["dispatched"] += 1
                else:
                    stats["skipped"] += 1
                rec = a.to_dict() | {"dispatched_to": dispatched_to}
                log_lines.append(json.dumps(rec, ensure_ascii=False))
        finally:
            if client is not None:
                client.close()
            if log_lines:
                with self._log_path.open("a", encoding="utf-8") as f:
                    f.write("\n".join(log_lines) + "\n")
                self._trim_log()
        return stats

    def _trim_log(self) -> None:
        """Keep only the last `ring_size * 5` lines on disk."""
        try:
            keep = self._ring_size * 5
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
            if len(lines) > keep:
                self._log_path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
        except Exception:
            pass

    def recent(self, limit: int = 50) -> list[dict]:
        """Read the last `limit` records from the JSONL audit log. Newest first."""
        if not self._log_path.exists():
            return []
        try:
            # Cheap tail: we don't expect millions of alerts. Read & slice.
            lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        out = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return list(reversed(out))

    # ------------------------------------------------------------------
    # channels (sync)
    # ------------------------------------------------------------------
    def _dashboard_link(self, alert: IncidentAlert) -> str | None:
        base = os.environ.get("BT_MONITOR_DASHBOARD_URL")
        if not base:
            return None
        return f"{base.rstrip('/')}/incidents?status=open&category={alert.category}"

    def _send_teams(self, client: httpx.Client, url: str, alert: IncidentAlert) -> bool:
        link = self._dashboard_link(alert)
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": alert.color_hex,
            "title": alert.title,
            "text": alert.summary,
            "sections": [{
                "facts": [
                    {"name": "Target",   "value": alert.target_url},
                    {"name": "Category", "value": alert.category},
                    {"name": "Severity", "value": alert.severity},
                    {"name": "Page",     "value": alert.page_url or "—"},
                    {"name": "Run",      "value": f"#{alert.run_id}"},
                ],
            }],
        }
        if link:
            card["potentialAction"] = [{
                "@type": "OpenUri",
                "name": "Open dashboard",
                "targets": [{"os": "default", "uri": link}],
            }]
        try:
            r = client.post(url, json=card)
            r.raise_for_status()
            return True
        except Exception as exc:
            log.warning("Teams webhook failed: %r", exc)
            return False

# Process-singleton — both runner and dashboard import this name.
hub = IncidentAlertHub()
