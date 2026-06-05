#!/usr/bin/env sh
# Container entrypoint: apply DB migrations, then start the app.
#
# In production the DB is Postgres (BT_MONITOR_DB_URL points at it). init_db()
# is a no-op for non-SQLite backends, so the schema is owned entirely by
# Alembic. `alembic upgrade head` is idempotent — safe to run on every boot;
# it applies any pending migrations and does nothing when already current.
set -e

echo "[entrypoint] running database migrations (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] starting uvicorn..."
exec python -m uvicorn dashboard.app:app \
    --host "${BT_MONITOR_HOST:-0.0.0.0}" \
    --port "${BT_MONITOR_PORT:-8000}"
