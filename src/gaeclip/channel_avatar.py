"""Channel avatar resolution: yt-dlp channel pages + local asset store.

Avatar sources, in priority order (spec §4):

1. Manual upload (``POST /api/v1/brand-assets/avatar/upload``) or explicit
   URL fetch — stored under ``storage/assets/avatars/``.
2. The YouTube channel page itself: ``yt-dlp --dump-single-json`` on the
   channel URL yields ``thumbnails`` entries whose ``id`` starts with
   ``avatar`` (``avatar_uncropped`` preferred). Downloaded once, then cached
   by channel id.
3. Fallback: no avatar (renderer hides the avatar, keeps the name).

Nothing here ever fails a job: every failure path returns None and the
caller falls back to name-only branding.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ASSETS_DIRNAME = "assets"
AVATAR_SUBDIR = "avatars"
MAX_AVATAR_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 2 * 1024 * 1024

_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
)


def assets_dir(storage_root: str | Path) -> Path:
    """Shared asset root (outside per-job dirs so deletes never wipe it)."""
    path = Path(storage_root).expanduser() / ASSETS_DIRNAME / AVATAR_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", (value or "").strip()).strip("-")[:64]


def pick_avatar_url(channel_json: dict[str, Any]) -> str | None:
    """Best avatar URL from a yt-dlp channel-page payload."""
    thumbs = channel_json.get("thumbnails") or []
    if not isinstance(thumbs, list):
        return None
    fallback: str | None = None
    for thumb in thumbs:
        if not isinstance(thumb, dict):
            continue
        url = thumb.get("url")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        thumb_id = str(thumb.get("id") or "")
        if thumb_id == "avatar_uncropped":
            return url
        if thumb_id.startswith("avatar") and fallback is None:
            fallback = url
    return fallback


def _download_image(url: str, dest: Path, max_bytes: int = MAX_AVATAR_BYTES) -> Path | None:
    """Download one image with type + size guards. None on any failure."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return None
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Gaeclip/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in ("image/png", "image/jpeg", "image/jpg", "image/webp"):
                logger.warning("brand avatar: unexpected content-type %s", ctype)
                return None
            data = response.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            return None
        ext = ".jpg"
        for magic, suffix in _IMAGE_MAGIC:
            if data.startswith(magic):
                ext = suffix
                break
        else:
            if ctype != "image/webp":
                logger.warning("brand avatar: unrecognized image magic")
                return None
            ext = ".webp"
        dest = dest.with_suffix(ext)
        dest.write_bytes(data)
        return dest
    except Exception as exc:
        logger.warning("brand avatar download failed (%s)", exc)
        return None


def fetch_channel_avatar(
    channel_id: str | None,
    channel_url: str | None,
    directory: str | Path,
    yt_dlp: str = "yt-dlp",
    max_age_days: float = 30,
) -> Path | None:
    """Resolve + cache a channel avatar. Pure fallback semantics (None ok).

    Cached files younger than ``max_age_days`` are reused without network.
    Stale files trigger one refresh attempt; if that fails the stale file
    is returned (better than nothing, never fails the caller).
    """
    directory = Path(directory)
    key = _safe_name(channel_id or "") or hashlib.sha256(
        str(channel_url or "").encode()).hexdigest()[:16]
    if not key:
        return None
    stale: Path | None = None
    for suffix in (".png", ".jpg", ".webp"):
        cached = directory / f"ch-{key}{suffix}"
        try:
            if cached.is_file() and cached.stat().st_size > 1024:
                try:
                    import time as _time

                    age_days = (_time.time() - cached.stat().st_mtime) / 86400.0
                except OSError:
                    return cached
                if age_days <= max_age_days:
                    return cached
                stale = cached
                break
        except OSError:
            continue
    url = (channel_url or "").strip()
    if not url and channel_id:
        url = f"https://www.youtube.com/channel/{channel_id}"
    if not url:
        return stale
    try:
        from .downloader import _ytdlp_base_args
        from .media import run_command

        result = run_command([
            yt_dlp, *_ytdlp_base_args(), "--skip-download",
            "--dump-single-json", "--no-warnings",
            "--playlist-end", "1", url,
        ], timeout=120)
        payload = json.loads(result.stdout)
    except Exception as exc:
        logger.warning("brand avatar: channel fetch failed (%s)", exc)
        return stale
    if not isinstance(payload, dict):
        return stale
    avatar_url = pick_avatar_url(payload)
    if not avatar_url:
        logger.warning("brand avatar: no avatar thumbnail on channel page")
        return stale
    fresh = _download_image(avatar_url, directory / f"ch-{key}.jpg")
    if fresh is None:
        return stale
    if stale is not None and stale != fresh:
        try:
            stale.unlink()
        except OSError:
            pass
    return fresh


def save_uploaded_avatar(data: bytes, filename: str, directory: str | Path) -> str:
    """Persist a manually uploaded avatar. Returns the asset id.

    Raises ValueError with a user-facing message on invalid input.
    """
    from .errors import ValidationError

    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"Avatar harus 1 byte–{MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    ext = ""
    for magic, suffix in _IMAGE_MAGIC:
        if data.startswith(magic):
            ext = suffix
            break
    if not ext:
        allowed = (Path(filename or "").suffix or "").lower()
        if allowed not in (".png", ".jpg", ".jpeg", ".webp"):
            raise ValidationError("Avatar harus PNG atau JPEG")
        ext = ".jpg" if allowed in (".jpg", ".jpeg") else allowed
    asset_id = f"manual-{uuid.uuid4().hex[:12]}{ext}"
    path = Path(directory) / asset_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return asset_id


def fetch_remote_avatar(url: str, directory: str | Path) -> str:
    """Download an explicit avatar URL into the store. Returns asset id."""
    from .errors import ValidationError

    asset_id = f"url-{uuid.uuid4().hex[:12]}.jpg"
    path = Path(directory) / asset_id
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = _download_image((url or "").strip(), path)
    if saved is None:
        raise ValidationError("Tidak dapat mengunduh avatar dari URL itu")
    if saved.name != asset_id:
        try:
            saved.rename(path.with_suffix(saved.suffix))
            asset_id = f"{path.stem}{saved.suffix}"
        except OSError:
            asset_id = saved.name
    return asset_id


def resolve_asset_path(asset_id: str, directory: str | Path) -> Path | None:
    """Strict asset-id → file mapping (traversal-proof)."""
    asset_id = (asset_id or "").strip()
    if not asset_id or "/" in asset_id or "\\" in asset_id:
        return None
    if _ASSET_ID_RE.match(asset_id) is None:
        return None
    try:
        base = Path(directory).resolve()
        candidate = (base / asset_id).resolve()
        if candidate.parent != base:
            return None
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None
