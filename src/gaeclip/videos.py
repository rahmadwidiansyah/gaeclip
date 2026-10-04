"""Canonical YouTube video metadata store (Redis-backed).

One VideoSource per youtube_video_id, shared by every job that processes
the same video. Jobs keep a point-in-time snapshot so history stays stable
even if the source video is renamed later.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class VideoSource:
    youtube_video_id: str
    url: str
    title: str | None = None
    channel_name: str | None = None
    thumbnail_url: str | None = None
    published_at: str | None = None
    duration: float | None = None
    created_at: float = 0
    updated_at: float = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def snapshot(self) -> dict[str, Any]:
        """Permanently attached copy stored on each JobRecord."""
        return {
            "youtube_video_id": self.youtube_video_id,
            "url": self.url,
            "title": self.title,
            "channel_name": self.channel_name,
            "thumbnail_url": self.thumbnail_url,
            "published_at": self.published_at,
            "duration": self.duration,
        }


def _pick_thumbnail(metadata: dict[str, Any]) -> str | None:
    thumbs = metadata.get("thumbnails") or []
    if not isinstance(thumbs, list) or not thumbs:
        return None
    # Prefer a mid-size thumbnail (~640px wide) for list display.
    best: dict[str, Any] | None = None
    for thumb in thumbs:
        if not isinstance(thumb, dict) or not thumb.get("url"):
            continue
        width = thumb.get("width") or 0
        if best is None or abs(width - 640) < abs((best.get("width") or 0) - 640):
            best = thumb
    return best["url"] if best else None


def _published_at(metadata: dict[str, Any]) -> str | None:
    for key in ("upload_date", "release_date"):
        raw = metadata.get(key)
        if isinstance(raw, str) and len(raw) == 8 and raw.isdigit():
            return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    timestamp = metadata.get("timestamp")
    if isinstance(timestamp, (int, float)):
        return time.strftime("%Y-%m-%d", time.gmtime(timestamp))
    return None


def build_snapshot(video_id: str, url: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """Map a yt-dlp metadata payload to a storable snapshot (NULL-tolerant)."""
    duration = metadata.get("duration")
    try:
        duration = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration = None
    title = metadata.get("title") or metadata.get("fulltitle")
    return {
        "youtube_video_id": video_id,
        "url": url,
        "title": title if isinstance(title, str) and title.strip() else None,
        "channel_name": metadata.get("channel") or metadata.get("uploader"),
        "thumbnail_url": _pick_thumbnail(metadata),
        "published_at": _published_at(metadata),
        "duration": duration,
    }


class VideoStore:
    """Deduplicated video metadata keyed by youtube_video_id."""

    def __init__(self, client: Any, prefix: str = "gaeclip"):
        self.client = client
        self.prefix = prefix

    def _key(self, video_id: str) -> str:
        return f"{self.prefix}:video:{video_id}"

    def _ids_key(self) -> str:
        return f"{self.prefix}:video:ids"

    def get(self, video_id: str) -> VideoSource | None:
        if not video_id:
            return None
        raw = self.client.get(self._key(video_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return VideoSource(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    def upsert(self, video_id: str, url: str, metadata: dict[str, Any]) -> VideoSource:
        """Insert or refresh a source. Never creates duplicates per video ID."""
        now = time.time()
        existing = self.get(video_id)
        snapshot = build_snapshot(video_id, url, metadata)
        record = VideoSource(
            youtube_video_id=video_id,
            url=url,
            title=snapshot["title"],
            channel_name=snapshot["channel_name"],
            thumbnail_url=snapshot["thumbnail_url"],
            published_at=snapshot["published_at"],
            duration=snapshot["duration"],
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.client.set(self._key(video_id), json.dumps(record.to_dict()))
        try:
            self.client.sadd(self._ids_key(), video_id)
        except (AttributeError, TypeError):
            pass
        return record

    def list_ids(self) -> list[str]:
        try:
            members = self.client.smembers(self._ids_key())
        except (AttributeError, TypeError):
            return []
        return sorted(
            member.decode() if isinstance(member, bytes) else str(member)
            for member in members
        )

    def list_all(self) -> list[VideoSource]:
        return [source for video_id in self.list_ids() if (source := self.get(video_id))]
