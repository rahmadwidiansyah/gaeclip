"""Store client contracts. Every Gaeclip store (jobs, settings,
transcripts, videos, providers, health) uses ONLY these operations, so a
single client implementation per backend is sufficient.

Both ``redis.Redis`` (server) and ``runtime.sqlite_client.SQLiteClient``
(desktop) satisfy these protocols structurally. Values are str/bytes on
both; readers must tolerate either (all existing stores already do).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class KeyValueOps(Protocol):
    def get(self, key: str) -> Any: ...
    def set(self, key: str, value: Any, ex: int | None = ..., nx: bool | None = ...) -> Any: ...
    def delete(self, *keys: str) -> Any: ...
    def ping(self) -> Any: ...


@runtime_checkable
class HashOps(Protocol):
    def hget(self, name: str, key: str) -> Any: ...
    def hset(self, name: str, key: str, value: Any = ..., mapping: dict | None = ...) -> Any: ...
    def hdel(self, name: str, *keys: str) -> Any: ...
    def hgetall(self, name: str) -> Any: ...


@runtime_checkable
class SetOps(Protocol):
    def sadd(self, name: str, *members: str) -> Any: ...
    def smembers(self, name: str) -> Any: ...


@runtime_checkable
class ScanOps(Protocol):
    def scan_iter(self, match: str = ..., count: int = ...) -> Any: ...
    def pipeline(self) -> Any: ...


@runtime_checkable
class CounterOps(Protocol):
    def incr(self, key: str) -> Any: ...
    def decr(self, key: str) -> Any: ...
    def expire(self, key: str, seconds: int) -> Any: ...


class StoreClient(KeyValueOps, HashOps, SetOps, ScanOps, CounterOps, Protocol):
    """Full contract required by Gaeclip stores."""
