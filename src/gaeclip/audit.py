"""Structured access/operation audit logger.

Appends one JSON line per auditable event to ``<STORAGE_ROOT>/audit.jsonl``.
The file is rotated when it exceeds ``AUDIT_MAX_BYTES`` (default 50 MiB);
rotation is atomic by rename so readers never see a half-written line.

Sensitive values (tokens, secrets, raw transcript text) are intentionally
never written here. Job IDs and IP addresses are recorded so incidents can
be traced back without exposing secrets.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gaeclip.audit")

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_ROTATION_HINT_KEY = "_gaeclip_audit_rotation"


def _get_audit_path(storage_root: Path | str | None = None) -> Path:
    root = Path(storage_root) if storage_root else Path(os.getenv("STORAGE_ROOT", "./storage"))
    return root.expanduser().resolve() / "audit.jsonl"


def _rotation_size(path: Path, max_bytes: int) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _rotate(path: Path) -> None:
    rotated = path.with_suffix(path.suffix + ".1")
    if rotated.exists():
        rotated.unlink()
    if path.exists():
        path.rename(rotated)


def write_event(
    *,
    storage_root: Path | str | None = None,
    action: str,
    actor: str = "anonymous",
    job_id: str | None = None,
    ip: str | None = None,
    details: dict[str, Any] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Append a single audit record. Does not raise on I/O errors."""
    try:
        path = _get_audit_path(storage_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if _rotation_size(path, max_bytes) >= max_bytes:
            _rotate(path)
        record = {
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S.%fZ", time.gmtime()),
            "action": action,
            "actor": actor,
            "job_id": job_id,
            "ip": ip,
            **(details or {}),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("audit write failed: %s", repr(exc))


def list_recent(storage_root: Path | str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Read the last N lines from the current audit file (not rotated)."""
    path = _get_audit_path(storage_root)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for raw in lines[-limit:]:
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def size_bytes(storage_root: Path | str | None = None) -> int:
    path = _get_audit_path(storage_root)
    try:
        return path.stat().st_size
    except OSError:
        return 0
