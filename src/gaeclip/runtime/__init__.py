"""Dual-runtime infrastructure: server (Redis + Celery) vs desktop (SQLite + local).

GAEClip Core (pipeline, curation, framing, TalkNet, rendering) is shared
and runtime-agnostic. ONLY this package plus the thin wiring in
``worker._redis`` / ``api.create_app`` differ per edition:

- server  (GAECLIP_RUNTIME unset or "server"): Redis + Celery, Docker.
  Behavior is byte-identical to every previous release.
- desktop (GAECLIP_RUNTIME=desktop): SQLite client shim + LocalRunner,
  Tauri installer. Never touches Redis, even if REDIS_URL is set.

Application code depends on the client op contract documented in
``runtime.abc`` — never on ``import redis`` directly.
"""

from .factory import is_desktop, is_server, make_client, make_runner
from .mode import RuntimeMode, current_runtime, storage_db_path

__all__ = [
    "RuntimeMode",
    "current_runtime",
    "is_desktop",
    "is_server",
    "make_client",
    "make_runner",
    "storage_db_path",
]
