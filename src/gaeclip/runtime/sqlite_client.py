"""SQLite-backed store client for the desktop runtime.

Implements the exact op contract in ``runtime.abc`` (same semantics as
the Redis calls Gaeclip stores already make), persisted in a single
``gaeclip.db`` inside STORAGE_ROOT. Thread-safe for the desktop worker
(single connection + lock; desktop concurrency is 1-2 jobs).

Semantics mirror redis-py closely enough that every existing store works
unchanged: str values (like the test FakeRedis), ``nx``/``ex`` on SET,
lazy expiry, glob SCAN, queued pipelines, integer counters.
"""

from __future__ import annotations

import fnmatch
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class _Pipeline:
    def __init__(self, client: "SQLiteClient"):
        self._client = client
        self._ops: list[tuple] = []

    def set(self, key: str, value: Any, ex: int | None = None, nx: bool | None = None):
        self._ops.append(("set", key, value, ex, nx))
        return self

    def hset(self, name: str, key: str, value: Any = None, mapping: dict | None = None):
        self._ops.append(("hset", name, key, value, mapping))
        return self

    def execute(self) -> list:
        with self._client._lock:
            return [self._client._apply(op) for op in self._ops]


class SQLiteClient:
    """Drop-in store client backed by SQLite (desktop edition)."""

    kind = "sqlite"

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS kv "
                "(key TEXT PRIMARY KEY, value TEXT, expires_at REAL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS hashes "
                "(name TEXT, key TEXT, value TEXT, PRIMARY KEY (name, key))"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS sets "
                "(name TEXT, member TEXT, PRIMARY KEY (name, member))"
            )
            self._conn.commit()

    # -- internals ------------------------------------------------------
    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _purge(self, key: str) -> None:
        now = time.time()
        row = self._conn.execute(
            "SELECT expires_at FROM kv WHERE key=?", (key,)).fetchone()
        if row is not None and row[0] is not None and row[0] <= now:
            self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
            self._conn.commit()

    def _apply(self, op: tuple) -> Any:
        if op[0] == "set":
            _, key, value, ex, nx = op
            return self._set(key, value, ex=ex, nx=nx)
        _, name, key, value, mapping = op
        return self._hset(name, key, value, mapping)

    # -- key/value ------------------------------------------------------
    def get(self, key: str) -> str | None:
        with self._lock:
            self._purge(key)
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set(self, key: str, value: Any, ex: int | None = None,
            nx: bool | None = None) -> bool:
        with self._lock:
            return self._set(key, value, ex=ex, nx=nx)

    def _set(self, key: str, value: Any, ex: int | None = None,
             nx: bool | None = None) -> bool:
        self._purge(key)
        if nx:
            exists = self._conn.execute(
                "SELECT 1 FROM kv WHERE key=?", (key,)).fetchone()
            if exists:
                return False
        expires_at = (time.time() + ex) if ex else None
        self._conn.execute(
            "INSERT INTO kv(key, value, expires_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "expires_at=excluded.expires_at",
            (key, self._text(value), expires_at),
        )
        self._conn.commit()
        return True

    def delete(self, *keys: str) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                cursor = self._conn.execute("DELETE FROM kv WHERE key=?", (key,))
                removed += cursor.rowcount
            self._conn.commit()
            return removed

    def incr(self, key: str) -> int:
        with self._lock:
            self._purge(key)
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            try:
                value = int(row[0]) if row else 0
            except (TypeError, ValueError):
                value = 0
            value += 1
            self._conn.execute(
                "INSERT INTO kv(key, value, expires_at) VALUES(?,?,NULL) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "expires_at=NULL",
                (key, str(value)),
            )
            self._conn.commit()
            return value

    def decr(self, key: str) -> int:
        with self._lock:
            self._purge(key)
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            try:
                value = int(row[0]) if row else 0
            except (TypeError, ValueError):
                value = 0
            value -= 1
            self._conn.execute(
                "INSERT INTO kv(key, value, expires_at) VALUES(?,?,NULL) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "expires_at=NULL",
                (key, str(value)),
            )
            self._conn.commit()
            return value

    def expire(self, key: str, seconds: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE kv SET expires_at=? WHERE key=?",
                (time.time() + seconds, key),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def ping(self) -> bool:
        with self._lock:
            self._conn.execute("SELECT 1")
        return True

    def scan_iter(self, match: str = "*", count: int = 100) -> list[str]:
        with self._lock:
            now = time.time()
            rows = self._conn.execute(
                "SELECT key FROM kv WHERE expires_at IS NULL OR expires_at > ?",
                (now,),
            ).fetchall()
            return [key for (key,) in rows if fnmatch.fnmatch(key, match)]

    def pipeline(self) -> _Pipeline:
        return _Pipeline(self)

    # -- hashes ----------------------------------------------------------
    def hget(self, name: str, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM hashes WHERE name=? AND key=?",
                (name, key)).fetchone()
            return row[0] if row else None

    def hset(self, name: str, key: str | None = None, value: Any = None,
             mapping: dict | None = None) -> int:
        with self._lock:
            return self._hset(name, key, value, mapping)

    def _hset(self, name: str, key: str | None, value: Any = None,
              mapping: dict | None = None) -> int:
        if mapping:
            for item_key, item_value in mapping.items():
                self._conn.execute(
                    "INSERT INTO hashes(name, key, value) VALUES(?,?,?) "
                    "ON CONFLICT(name, key) DO UPDATE SET value=excluded.value",
                    (name, self._text(item_key), self._text(item_value)),
                )
            self._conn.commit()
            return len(mapping)
        if key is None:
            raise TypeError("hset() requires key or mapping")
        self._conn.execute(
            "INSERT INTO hashes(name, key, value) VALUES(?,?,?) "
            "ON CONFLICT(name, key) DO UPDATE SET value=excluded.value",
            (name, self._text(key), self._text(value)),
        )
        self._conn.commit()
        return 1

    def hdel(self, name: str, *keys: str) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                cursor = self._conn.execute(
                    "DELETE FROM hashes WHERE name=? AND key=?", (name, key))
                removed += cursor.rowcount
            self._conn.commit()
            return removed

    def hgetall(self, name: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM hashes WHERE name=?", (name,)).fetchall()
            return {key: value for key, value in rows}

    # -- sets -------------------------------------------------------------
    def sadd(self, name: str, *members: str) -> int:
        with self._lock:
            added = 0
            for member in members:
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO sets(name, member) VALUES(?,?)",
                    (name, self._text(member)),
                )
                added += cursor.rowcount
            self._conn.commit()
            return added

    def smembers(self, name: str) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT member FROM sets WHERE name=?", (name,)).fetchall()
            return {member for (member,) in rows}
