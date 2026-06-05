"""Centralized logging configuration (#22).

One place to set log level + format, driven by env so ops can turn up verbosity
in production without code changes:

    BT_MONITOR_LOG_LEVEL    DEBUG | INFO | WARNING | ERROR   (default INFO)
    BT_MONITOR_LOG_FORMAT   "text" (default) | "json"

Secrets policy: webhook URLs and credentials come ONLY from environment
variables (never committed). If any secret-bearing value must be logged, route
it through `mask_secret()` so it never lands in a log in clear text.
"""
from __future__ import annotations

import logging
import os

_CONFIGURED = False

_TEXT_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s"
_JSON_FORMAT = (
    '{"ts":"%(asctime)s","level":"%(levelname)s",'
    '"logger":"%(name)s","msg":"%(message)s"}'
)


def configure_logging() -> None:
    """Idempotent. Call once at process startup (dashboard + runner)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("BT_MONITOR_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    fmt = _JSON_FORMAT if os.environ.get("BT_MONITOR_LOG_FORMAT", "text").lower() == "json" else _TEXT_FORMAT

    root = logging.getLogger()
    # Replace any handlers installed by basicConfig elsewhere so format is uniform.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


def mask_secret(value: str | None, keep: int = 4) -> str:
    """Mask a secret for safe logging: show only the last `keep` chars."""
    if not value:
        return "(unset)"
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return "*" * (len(s) - keep) + s[-keep:]
