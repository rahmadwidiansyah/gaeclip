"""YouTube cookie resolution with edition parity.

The pipeline consumes cookies ONLY through ``YTDLP_COOKIES_FILE``
(downloader reads the env var). Resolution order below preserves server
behavior exactly (explicit env wins) while giving desktop users a
Settings-managed file:

1. ``YTDLP_COOKIES_FILE`` env, when set and non-blank (both editions).
2. ``<STORAGE_ROOT>/cookies.txt`` when present (uploaded via Settings;
   both editions, opt-in).
3. None (previous behavior when nothing is configured).
"""

from __future__ import annotations

import os
from pathlib import Path

MANAGED_FILENAME = "cookies.txt"


def managed_cookies_path(storage_root: str | Path | None = None) -> Path:
    if storage_root is None:
        storage_root = os.getenv("STORAGE_ROOT", "./storage")
    return Path(storage_root).expanduser() / MANAGED_FILENAME


def resolve_cookies_file(storage_root: str | Path | None = None) -> str | None:
    """Effective cookie file path, or None when unconfigured."""
    explicit = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if explicit:
        return explicit
    managed = managed_cookies_path(storage_root)
    try:
        if managed.is_file() and managed.stat().st_size > 0:
            return str(managed)
    except OSError:
        pass
    return None


def export_cookies_file() -> str | None:
    """Publish the resolved path into the process env for yt-dlp.

    Desktop startup calls this so the pipeline (which only reads the env
    var) picks up Settings-managed cookies without code changes.
    Returns the path, or None when unconfigured.
    """
    resolved = resolve_cookies_file()
    if resolved:
        os.environ["YTDLP_COOKIES_FILE"] = resolved
    return resolved


def save_managed_cookies(text: str, storage_root: str | Path | None = None) -> Path:
    """Store Settings-provided cookie text (Netscape format). Validates
    minimally: non-empty and looks like a cookies file."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Cookie file is empty")
    if "Netscape HTTP Cookie File" not in cleaned and "\t" not in cleaned:
        raise ValueError("Not a Netscape cookies.txt format")
    path = managed_cookies_path(storage_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cleaned + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
