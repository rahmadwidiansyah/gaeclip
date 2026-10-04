"""Backend selection. Server behavior is identical to every previous
release; desktop can never touch Redis (even if REDIS_URL is set)."""

from __future__ import annotations

import logging
import os

from .mode import RuntimeMode, current_runtime, storage_db_path

logger = logging.getLogger(__name__)


def is_server() -> bool:
    return current_runtime() is RuntimeMode.SERVER


def is_desktop() -> bool:
    return current_runtime() is RuntimeMode.DESKTOP


def make_client(storage_root: str | None = None):
    """Store client for the active runtime.

    Server: ``redis.Redis`` from REDIS_URL (unchanged default URL).
    Desktop: SQLite shim in STORAGE_ROOT. REDIS_URL is deliberately
    ignored here and warned about, so the desktop app can never
    accidentally connect to an external Redis server.
    """
    if is_desktop():
        if os.getenv("REDIS_URL"):
            logger.warning(
                "GAECLIP_RUNTIME=desktop: ignoring REDIS_URL, using local SQLite store")
        from .sqlite_client import SQLiteClient

        return SQLiteClient(storage_db_path(storage_root))
    import redis

    return redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"),
        decode_responses=False,
    )


def make_runner():
    """Job runner for the active runtime (server=Celery, desktop=local)."""
    from .runners import CeleryJobRunner, LocalJobRunner

    if is_desktop():
        return LocalJobRunner()
    return CeleryJobRunner()
