"""Pytest fixtures to clean up global state between tests.

Modules like ``gaeclip.rate_limiter``, ``gaeclip.metrics``, and
``gaeclip.audit`` keep module-level state. Without resetting them,
later tests may see leftover buckets/counters/log files and fail
 spuriously.
"""

import pytest

@pytest.fixture(autouse=True)
def cleanup_global_state():
    yield
    # Rate limiter
    try:
        from gaeclip.rate_limiter import reset as reset_rate_limiter
        reset_rate_limiter()
    except Exception:
        pass
    # Metrics
    try:
        from gaeclip.metrics import reset as reset_metrics
        reset_metrics()
    except Exception:
        pass
    # Audit log file (remove temp files created during tests)
    try:
        from gaeclip.audit import _get_audit_path
        import tempfile
        # Only remove if path is under /tmp or current test dir.
        # We don't want to delete production audit logs.
        path = _get_audit_path()
        if path.exists() and any(part in ("tmp", "test") for part in path.parts):
            path.unlink(missing_ok=True)
            rotated = path.with_suffix(path.suffix + ".1")
            rotated.unlink(missing_ok=True)
    except Exception:
        pass
