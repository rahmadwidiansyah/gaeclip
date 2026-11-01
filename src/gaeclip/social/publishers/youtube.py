"""YouTube publisher (official YouTube Data API v3).

Capabilities (honest): direct publish, privacy, native scheduling
(publishAt), status polling via processingDetails, multiple channels.
Uploads the FINAL GAEClip MP4 with resumable sessions — no re-encode.

Default title/description come from the publish request (clip.title);
the user edits them in the Publish dialog before submitting.

OAuth scopes (documented):
- https://www.googleapis.com/auth/youtube.upload   (upload + set metadata)
- https://www.googleapis.com/auth/youtube.readonly (channel label for accounts)
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import AccountInfo, PublishContext, SocialPublisher, validate_clip_file
from ..oauth import OAuthProviderConfig
from ..states import (AccountRequirementError, AuthError, MediaError,
                      ProviderCapabilities, PublishRequest, PublishResult,
                      SocialError, TransientError, classify_http_status)

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
CHUNK_BYTES = 8 * 1024 * 1024
MAX_BYTES = 256 * 1024 * 1024 * 1024  # 256 GiB (verified uploads far smaller)

YOUTUBE_OAUTH = OAuthProviderConfig(
    provider="youtube",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scopes=("https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly"),
    extra_authorize_params={"access_type": "offline", "prompt": "consent"},
    use_pkce=True,
    client_secret_required=False,  # installed-app (desktop) clients
)

CAPABILITIES = ProviderCapabilities(
    direct_publish=True, draft_upload=False, scheduling=True,
    thumbnail=False, title=True, description=True, caption=False,
    hashtags=False, privacy=True, status_polling=True,
    multiple_accounts=True,
    supports_upload_progress=True,
    supports_processing_status=True,
    supports_remote_status_polling=True,
    supports_provider_url=True,
    supports_cancel=False,
    supports_publish_verification=True,
)


class YouTubePublisher(SocialPublisher):
    provider = "youtube"
    capabilities = CAPABILITIES

    # -- HTTP ----------------------------------------------------------
    @staticmethod
    def _request(method: str, url: str, access_token: str,
                 body: bytes | None = None,
                 headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {access_token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return (response.status,
                        {k.lower(): v for k, v in response.headers.items()},
                        response.read())
        except urllib.error.HTTPError as exc:
            resp_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
            try:
                payload = exc.read()
            except Exception:
                payload = b""
            raise _YoutubeHttpError(exc.code, payload, resp_headers) from exc
        except OSError as exc:
            raise TransientError(f"youtube unreachable: {exc}") from exc

    @staticmethod
    def _json(method: str, url: str, access_token: str,
              payload: dict | None = None) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            status, _, raw = YouTubePublisher._request(
                method, url, access_token, body,
                {"Content-Type": "application/json; charset=UTF-8"} if body else None)
        except _YoutubeHttpError as exc:
            raise classify_http_status(exc.status, _error_message(exc.payload)) from exc
        if status >= 400:
            raise classify_http_status(status, raw.decode("utf-8", "replace")[:300])
        try:
            parsed = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise TransientError("youtube returned invalid JSON") from exc
        return parsed if isinstance(parsed, dict) else {}

    # -- accounts ------------------------------------------------------
    def account_info(self, access_token: str) -> AccountInfo:
        data = self._json("GET", f"{API_BASE}/channels?mine=true&part=id,snippet",
                          access_token)
        items = data.get("items") or []
        if not items:
            raise AccountRequirementError("no YouTube channel on this Google account")
        channel = items[0]
        return AccountInfo(
            external_account_id=str(channel.get("id", "")),
            display_name=str((channel.get("snippet") or {}).get("title", "")),
            extra={"channel_id": channel.get("id", "")})

    def validate_account(self, access_token: str, account_meta: dict) -> None:
        # Same call proves the channel still exists + token works.
        self.account_info(access_token)

    # -- publish -------------------------------------------------------
    def publish(self, ctx: PublishContext) -> PublishResult:
        from ..states import PROCESSING, PUBLISHED, SCHEDULED

        validate_clip_file(ctx.clip_path, max_bytes=MAX_BYTES)
        token = ctx.access_token()
        metadata = self._build_metadata(ctx.request)
        total = ctx.clip_path.stat().st_size

        session_uri = self._init_resumable(token, metadata, total)
        video_id = self._upload_chunks(ctx, token, session_uri, total)
        logger.info("[SOCIAL_PUBLISH] provider=youtube clip_id=%s status=uploaded video=%s",
                    ctx.request.clip_id, video_id)
        # Persist the provider object IMMEDIATELY: if the status check below
        # times out, the service reconciles instead of duplicating the upload.
        try:
            ctx.on_uploaded(video_id, f"https://youtu.be/{video_id}")
        except Exception:
            pass
        ctx.on_progress(1.0, "uploaded")
        if ctx.request.scheduled_at:
            return PublishResult(status=SCHEDULED, remote_media_id=video_id,
                                 remote_url=f"https://youtu.be/{video_id}")
        # Fresh uploads may still be processing server-side; the service
        # polls get_publish_status() when status_polling is available.
        state = self.get_publish_status(token, video_id)
        final = PROCESSING if state == PROCESSING else PUBLISHED
        ctx.on_progress(1.0, "processing" if final == PROCESSING else "published")
        return PublishResult(status=final, remote_media_id=video_id,
                             remote_url=f"https://youtu.be/{video_id}")

    def get_publish_status(self, access_token: str, remote_media_id: str) -> str:
        from ..states import FAILED, PROCESSING, PUBLISHED

        data = self._json(
            "GET", f"{API_BASE}/videos?id={urllib.parse.quote(remote_media_id)}"
                   "&part=status,processingDetails", access_token)
        items = data.get("items") or []
        if not items:
            raise SocialError("youtube video not found", code="not_found",
                              user_message="Video tidak ditemukan di YouTube.")
        item = items[0]
        if (item.get("status") or {}).get("uploadStatus") == "failed":
            return FAILED
        processing = (item.get("processingDetails") or {}).get("processingStatus")
        if processing and processing != "succeeded":
            return PROCESSING
        return PUBLISHED

    # -- resumable protocol --------------------------------------------
    def _build_metadata(self, request: PublishRequest) -> dict[str, Any]:
        snippet: dict[str, Any] = {
            "title": request.title[:100] or "GAEClip",
            "description": request.caption[:5000],
            "categoryId": "22",  # People & Blogs; Shorts need no category
        }
        status: dict[str, Any] = {
            "privacyStatus": request.privacy if request.privacy in (
                "public", "unlisted", "private") else "public",
            "selfDeclaredMadeForKids": False,
        }
        if request.scheduled_at:
            publish_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(request.scheduled_at))
            status["publishAt"] = publish_at
            status["privacyStatus"] = "private"
        return {"snippet": snippet, "status": status}

    def _init_resumable(self, token: str, metadata: dict, total: int) -> str:
        url = (f"{UPLOAD_BASE}/videos?uploadType=resumable"
               "&part=snippet,status")
        body = json.dumps(metadata).encode("utf-8")
        try:
            status, headers, _ = self._request(
                "POST", url, token, body,
                {"Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Length": str(total),
                 "X-Upload-Content-Type": "video/mp4"})
        except _YoutubeHttpError as exc:
            raise classify_http_status(exc.status, _error_message(exc.payload)) from exc
        if status not in (200, 201):
            raise classify_http_status(status, "resumable init rejected")
        session_uri = headers.get("location", "")
        if not session_uri:
            raise TransientError("youtube omitted resumable session location")
        return session_uri

    def _upload_chunks(self, ctx: PublishContext, token: str,
                       session_uri: str, total: int) -> str:
        offset = 0
        with open(ctx.clip_path, "rb") as handle:
            while offset < total:
                ctx.check_cancelled()
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                try:
                    status, headers, raw = self._request(
                        "PUT", session_uri, token, chunk,
                        {"Content-Type": "video/mp4",
                         "Content-Length": str(len(chunk)),
                         "Content-Range": f"bytes {offset}-{end}/{total}"})
                except _YoutubeHttpError as exc:
                    # HTTP 308 "Resume Incomplete" is the NORMAL resumable-
                    # upload continuation signal from YouTube.  Python's
                    # urllib treats every non-2xx response as an HTTPError,
                    # so 308 arrives here instead of as a regular return.
                    # Extract the Range header and continue uploading.
                    if exc.status == 308:
                        received = exc.payload_headers.get("range", "")
                        offset = _resume_offset(received, offset + len(chunk))
                        ctx.on_progress(min(1.0, offset / total), "uploading")
                        continue
                    if exc.status in (500, 502, 503, 504):
                        raise TransientError(
                            f"youtube chunk failed, resume at {offset}") from exc
                    raise classify_http_status(exc.status, _error_message(exc.payload)) from exc
                if status in (200, 201):
                    try:
                        return str(json.loads(raw.decode("utf-8")).get("id", ""))
                    except ValueError as exc:
                        raise TransientError("youtube upload finale invalid") from exc
                if status == 308:
                    received = headers.get("range", "")
                    offset = _resume_offset(received, offset + len(chunk))
                    ctx.on_progress(min(1.0, offset / total), "uploading")
                    continue
                raise classify_http_status(status, raw.decode("utf-8", "replace")[:300])
        raise MediaError("upload ended without a YouTube video id")


class _YoutubeHttpError(Exception):
    def __init__(self, status: int, payload: bytes,
                 headers: dict[str, str] | None = None):
        super().__init__(f"youtube http {status}")
        self.status = status
        self.payload = payload
        self.payload_headers = headers or {}


def _error_message(payload: bytes) -> str:
    """First API error message only (user-safe: ids + reasons, no tokens)."""
    try:
        data = json.loads(payload.decode("utf-8") or "{}")
        items = ((data.get("error") or {}).get("errors") or [])
        if items:
            first = items[0]
            return f"{first.get('reason', '')}: {first.get('message', '')}"[:300]
        return str((data.get("error") or {}).get("message", ""))[:300]
    except ValueError:
        return payload.decode("utf-8", "replace")[:300]


def _resume_offset(range_header: str, optimistic: int) -> int:
    """Parse 'bytes=0-8388607' -> next byte; fall back to optimistic."""
    try:
        span = range_header.split("=", 1)[1]
        return int(span.split("-", 1)[1]) + 1
    except (IndexError, ValueError):
        return optimistic
