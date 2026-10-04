"""Explicit runtime selection. Server is the default so the desktop
edition can never activate by accident; unknown values fail fast."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path


class RuntimeMode(str, Enum):
    SERVER = "server"
    DESKTOP = "desktop"


ENV_VAR = "GAECLIP_RUNTIME"
DB_FILENAME = "gaeclip.db"


def current_runtime() -> RuntimeMode:
    """Unset/blank -> SERVER (existing behavior preserved).

    Raises ConfigurationError on unknown values instead of guessing,
    so a typo can never silently switch the storage backend.
    """
    from ..errors import ConfigurationError

    raw = os.getenv(ENV_VAR, "").strip().lower()
    if not raw:
        return RuntimeMode.SERVER
    try:
        return RuntimeMode(raw)
    except ValueError:
        raise ConfigurationError(
            f"{ENV_VAR} must be 'server' or 'desktop', got {raw!r}"
        ) from None


def storage_db_path(storage_root: str | Path | None = None) -> Path:
    """SQLite file for desktop mode. Lives inside STORAGE_ROOT so no new
    path concept is introduced (same dir jobs/transcripts already use)."""
    if storage_root is None:
        storage_root = os.getenv("STORAGE_ROOT", "./storage")
    return Path(storage_root).expanduser() / DB_FILENAME
