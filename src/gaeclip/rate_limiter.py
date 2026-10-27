"""Simple per-IP/token rate limiter for mutating API endpoints.

Uses an in-memory sliding window. Because the API is typically single-
process (desktop-first) and stateless across restarts is acceptable for
quotas, this is sufficient. For multi-instance deployments operators
should switch to a Redis-backed limiter or external gateway.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

logger = logging.getLogger("gaeclip.rate_limiter")

# Internal bucket: list of timestamps.
_Bucket = list[float]
_LimitStore = dict[str, _Bucket]


class RateLimiter:
    def __init__(self, default_limit: int = 60, window_seconds: float = 60.0) -> None:
        self.default_limit = default_limit
        self.window_seconds = window_seconds
        self._limits: _LimitStore = defaultdict(list)

    def key_for(self, request: Any) -> str:
        """Derive a stable identifier from the ASGI scope / request."""
        # Prefer explicit X-Client-Id header (set by auth middleware when
        # a bearer token is present) before falling back to IP.
        client_id = None
        headers = getattr(request, "headers", {}) or {}
        for key, value in headers.items():
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            if key.lower() == "x-client-id":
                client_id = value
                break
        if client_id:
            return f"client:{client_id}"
        peer = getattr(request, "client", None)
        if peer:
            return f"ip:{peer.host}"
        return "unknown"

    def allow(self, request: Any, limit: int | None = None) -> bool:
        limit = limit if limit is not None else self.default_limit
        now = time.time()
        key = self.key_for(request)
        window_start = now - self.window_seconds
        bucket = self._limits[key]
        # Prune old entries.
        while bucket and bucket[0] < window_start:
            bucket.pop(0)
        if len(bucket) >= limit:
            logger.warning("rate limit exceeded: key=%s count=%d limit=%d", key, len(bucket), limit)
            return False
        bucket.append(now)
        return True

    def remaining(self, request: Any, limit: int | None = None) -> int:
        limit = limit if limit is not None else self.default_limit
        now = time.time()
        key = self.key_for(request)
        window_start = now - self.window_seconds
        bucket = self._limits.get(key, [])
        active = [t for t in bucket if t >= window_start]
        self._limits[key] = active
        return max(0, limit - len(active))


# Module-level singleton used by the middleware.
_default = RateLimiter()


def get_limiter() -> RateLimiter:
    return _default


def reset() -> None:
    """For tests only."""
    _default._limits.clear()
