# BT Monitor — production image (#13).
# Multi-stage isn't worth it here (Chromium dominates size); single stage,
# slim base, non-root user.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PATCHRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app

# 1) Python deps first (cached layer).
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt \
    && pip install "psycopg[binary]>=3.2"   # Postgres driver for production

# 2) Chromium + its OS libraries for patchright (the deep scan needs a browser).
RUN patchright install --with-deps chromium

# 3) App code.
COPY . .

# 4) Non-root.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data && chown -R appuser:appuser /app /opt/ms-playwright
USER appuser

EXPOSE 8000

# Liveness: the unauthenticated /healthz route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# Bind 0.0.0.0 inside the container; a reverse proxy / compose network fronts it.
ENV BT_MONITOR_HOST=0.0.0.0 BT_MONITOR_PORT=8000
CMD ["python", "-m", "uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8000"]
