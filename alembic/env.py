"""Alembic migration environment for bt-monitor.

The target schema is `db.models.Base.metadata`. The database URL is resolved
exactly like the application does:

    BT_MONITOR_DB_URL  (e.g. postgresql+psycopg://user:pw@host:5432/bt_monitor)
    → falls back to the SQLite dev default (data/bt_monitor.db)

So `alembic upgrade head` targets whatever the app would use. In production
set BT_MONITOR_DB_URL to the Postgres instance and run migrations on deploy.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

# Make the project importable (alembic runs from the repo root).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from db.models import Base, _default_db_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _db_url() -> str:
    """Same resolution as db.models.get_engine()."""
    return os.environ.get("BT_MONITOR_DB_URL") or _default_db_url()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no live DB connection)."""
    url = _db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=url.startswith("sqlite"),
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (live DB connection)."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _db_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # batch mode lets SQLite do ALTER TABLE (it can't natively); harmless
            # elsewhere — we only enable it for SQLite.
            render_as_batch=is_sqlite,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
