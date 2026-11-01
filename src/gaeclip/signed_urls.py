"""Signed URL generator for clip/media downloads.

Creates HMAC-based query parameters that encode an expiry timestamp.
Useful for sharing clips without exposing the bearer token, or for
backend-to-backend transfers where token exchange is undesirable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("gaeclip.signed_urls")

DEFAULT_EXPIRY_SECONDS = 3600


def sign(
    *,
    secret: str,
    path: str,
    expires_in: float = DEFAULT_EXPIRY_SECONDS,
) -> str:
    """Return a signed URL string with ?sig=...&expires=..."""
    expires = int(time.time()) + int(expires_in)
    payload = json.dumps({"path": path, "exp": expires}, ensure_ascii=False)
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{path}?sig={sig}&expires={expires}"


def verify(*, secret: str, path: str, sig: str, expires: str) -> bool:
    try:
        exp = int(expires)
    except (TypeError, ValueError):
        return False
    if exp < time.time():
        return False
    payload = json.dumps({"path": path, "exp": exp}, ensure_ascii=False)
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def signed_download_url(
    *,
    storage_root: Path | str,
    job_id: str,
    clip_id: str,
    secret: str,
    expires_in: float = DEFAULT_EXPIRY_SECONDS,
) -> str:
    base = f"/api/v1/jobs/{job_id}/clips/{clip_id}"
    return sign(secret=secret, path=base, expires_in=expires_in)
