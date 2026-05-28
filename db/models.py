"""SQLAlchemy models for bt-monitor incident history.

Three tables:
  - runs                  : one row per runner invocation
  - incidents             : one row per distinct issue (deduped by fingerprint
                            within a target_url). Tracks first_seen / last_seen
                            / resolved_at — i.e. the lifetime of an issue across
                            multiple runs.
  - incident_observations : link table: which incident was seen in which run.
                            Powers the "show every time this was detected" view.

Default backend is SQLite at data/bt_monitor.db, override with the
BT_MONITOR_DB_URL env var (e.g. postgresql+psycopg://user:pw@host/db).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session


_HERE = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _HERE / "data" / "bt_monitor.db"


def _default_db_url() -> str:
    _DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Use POSIX-style path so SQLite URL is portable on Windows.
    return f"sqlite:///{_DEFAULT_DB_PATH.as_posix()}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    base_url: Mapped[str] = mapped_column(String(512), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    health_score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    summary: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    observations: Mapped[list["IncidentObservation"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Run {self.id} {self.base_url} {self.started_at} status={self.status}>"


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("target_url", "fingerprint", name="uq_incident_target_fp"),
        Index("ix_incident_target_status", "target_url", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_url: Mapped[str] = mapped_column(String(512), index=True)
    fingerprint: Mapped[str] = mapped_column(String(512), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(32), default="moderate", index=True)
    page_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    summary: Mapped[str] = mapped_column(String(1024))
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    first_seen_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    last_seen_run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("runs.id"), nullable=True)

    observations: Mapped[list["IncidentObservation"]] = relationship(
        back_populates="incident", cascade="all, delete-orphan"
    )

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def __repr__(self) -> str:
        state = "open" if self.is_open else "resolved"
        return f"<Incident {self.id} {self.category}/{self.severity} {state} fp={self.fingerprint[:40]}>"


class UptimeCheck(Base):
    """One row per live-watcher probe. Used for SLA calc + downtime detection.

    Append-only; the watcher rolls old rows out via a periodic prune (keep ~30 days).
    """
    __tablename__ = "uptime_checks"
    __table_args__ = (
        Index("ix_uptime_target_time", "target_url", "checked_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_url: Mapped[str] = mapped_column(String(512), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    state: Mapped[str] = mapped_column(String(16))   # "up" | "slow" | "down"
    status_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    response_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)


class IncidentObservation(Base):
    __tablename__ = "incident_observations"
    __table_args__ = (
        UniqueConstraint("incident_id", "run_id", name="uq_obs_incident_run"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    incident: Mapped[Incident] = relationship(back_populates="observations")
    run: Mapped[Run] = relationship(back_populates="observations")


_engine = None
_SessionFactory: Optional[sessionmaker[Session]] = None


def get_engine():
    global _engine, _SessionFactory
    if _engine is None:
        url = os.environ.get("BT_MONITOR_DB_URL") or _default_db_url()
        _engine = create_engine(url, future=True)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session() -> Session:
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    engine = get_engine()
    Base.metadata.create_all(engine)
