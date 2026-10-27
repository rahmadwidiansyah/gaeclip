"""TikTok publisher (official Content Posting API v2).

HONEST CAPABILITY DEFAULTS (spec §12/§51): direct posting requires an
audited TikTok app with the video.publish scope; unaudited/sandbox apps
only reach private/draft-grade posting. Defaults reflect that:

- direct_publish=False, draft_upload=True
- approval_note explains the audit requirement in UI.

Flows implemented (open.tiktokapis.com, all JSON except chunk PUTs):
- FILE_UPLOAD: init -> PUT chunks -> finish via /post/publish/
- DIRECT_POST vs DRAFT chosen from capabilities at publish time; the UI
  never labels a draft "Published" (states DRAFT vs PUBLISHED).

OAuth note: TikTok Login Kit redirect URIs are registered in the TikTok
developer portal. Loopback (127.0.0.1) is attempted; if TikTok rejects
it, the provider error surfaces — never a fake success.

STATUS: implemented against public docs, NOT integration-tested (no
TikTok client credentials available). See final report.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import AccountInfo, PublishContext, SocialPublisher, validate_clip_file
from ..oauth import OAuthProviderConfig
from ..states import (AccountRequirementError, ApprovalRequiredError,
                      ProviderCapabilities, PublishResult, SocialError,
                      TransientError, classify_http_status)

logger = logging.getLogger(__name__)

API_BASE = "https://open.tiktokapis.com/v2"
CHUNK_BYTES = 10 * 1024 * 1024
MAX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

TIKTOK_OAUTH = OAuthProviderConfig(
    provider="tiktok",
    authorize_url="https://www.tiktok.com/v2/auth/authorize/",
    token_url="https://open.tiktokapis.com/v2/oauth/token/",
    scopes=("user.info.basic", "video.upload"),
    extra_authorize_params={},
    # Login Kit names the identity field "client_key" (not "client_id").
    client_id_param="client_key",
    # PKCE support in Login Kit is unverified; state validation still
    # enforced. Do NOT send code_challenge until confirmed.
    use_pkce=False,
    # Confidential model: token endpoint REQUIRES client_key + client_secret.
    # The secret is always the USER's own app credential from Settings
    # (encrypted at rest), never a shared secret shipped in the binary.
    client_secret_required=True,
)

CAPABILITIES = ProviderCapabilities(
    direct_publish=False,  # flipped per-account after audit approval
    draft_upload=True,
    scheduling=False, thumbnail=False, title=False,
    description=False, caption=True, hashtags=True,
    privacy=True, status_polling=True, multiple_accounts=True,
    approval_note=("Direct posting requires an audited TikTok app "
                   "(video.publish scope). Until approved, uploads land as drafts."),
    supports_upload_progress=True,
    supports_processing_status=True,
    supports_remote_status_polling=True,
    supports_provider_url=False,
    supports_cancel=False,
    supports_publish_verification=False,
)


class TikTokPublisher(SocialPublisher):
    provider = "tiktok"
    capabilities = CAPABILITIES

    def __init__(self, *, allow_direct_post: bool = False):
        # Per-account override once audit approval is confirmed.
        self._allow_direct = allow_direct_post
        self.capabilities = ProviderCapabilities(
            **{**self.capabilities.__dict__, "direct_publish": allow_direct_post})

    # -- HTTP ----------------------------------------------------------
    @staticmethod
    def _api(method: str, path: str, access_token: str,
             payload: dict | None = None) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"Authorization": f"Bearer {access_token}",
                                                  "Content-Type": "application/json; charset=UTF-8"})
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise classify_http_status(exc.code, detail) from exc
        except OSError as exc:
            raise TransientError(f"tiktok unreachable: {exc}") from exc
        try:
            data = json.loads(raw or "{}")
        except ValueError as exc:
            raise TransientError("tiktok returned invalid JSON") from exc
        _raise_tiktok_error(data)
        return data.get("data", data) if isinstance(data, dict) else {}

    # -- accounts ------------------------------------------------------
    def account_info(self, access_token: str) -> AccountInfo:
        data = self._api("POST", "/oauth/userinfo/", access_token, {})
        info = data.get("data", data) if isinstance(data, dict) else {}
        return AccountInfo(
            external_account_id=str(info.get("open_id", "")),
            display_name=str(info.get("display_name", "") or info.get("username", "")),
            extra={"username": info.get("username", "")})

    def validate_account(self, access_token: str, account_meta: dict) -> None:
        info = self.account_info(access_token)
        if not info.external_account_id:
            raise AccountRequirementError("tiktok account not resolvable")

    # -- publish -------------------------------------------------------
    def publish(self, ctx: PublishContext) -> PublishResult:
        from ..states import DRAFT, PUBLISHED

        validate_clip_file(ctx.clip_path, max_bytes=MAX_BYTES)
        token = ctx.access_token()
        total = ctx.clip_path.stat().st_size
        publish_id, upload_url = self._init_file_upload(
            token, ctx.request.caption, total)
        self._upload_chunks(ctx, upload_url, total)
        try:
            ctx.on_progress(1.0, "uploaded")
        except Exception:
            pass
        if self._allow_direct:
            remote_id = self._finish_direct_post(token, publish_id, ctx.request)
            try:
                ctx.on_uploaded(remote_id, "")
            except Exception:
                pass
            logger.info("[SOCIAL_PUBLISH] provider=tiktok clip_id=%s status=published",
                        ctx.request.clip_id)
            return PublishResult(status=PUBLISHED, remote_media_id=remote_id)
        remote_id = self._finish_draft(token, publish_id, ctx.request)
        try:
            ctx.on_uploaded(remote_id, "")
        except Exception:
            pass
        logger.info("[SOCIAL_PUBLISH] provider=tiktok clip_id=%s status=draft",
                    ctx.request.clip_id)
        return PublishResult(status=DRAFT, remote_media_id=remote_id)

    def get_publish_status(self, access_token: str, remote_media_id: str) -> str:
        from ..states import FAILED, PROCESSING, PUBLISHED

        try:
            data = self._api("POST", "/post/publish/status/fetch/", access_token,
                             {"publish_id": remote_media_id})
        except SocialError:
            raise
        status = str(data.get("status", "")).upper()
        if status in ("PUBLISH_COMPLETE", "PUBLISHED"):
            return PUBLISHED
        if status in ("FAILED", "PUBLISH_FAILED"):
            return FAILED
        return PROCESSING

    # -- Content Posting API calls -------------------------------------
    def _init_file_upload(self, token: str, caption: str, total: int) -> tuple[str, str]:
        import math

        chunks = max(1, math.ceil(total / CHUNK_BYTES))
        data = self._api("POST", "/post/publish/video/init/", token, {
            "post_info": {"title": caption[:2200]},
            "source_info": {"source": "FILE_UPLOAD", "video_size": total,
                            "chunk_size": CHUNK_BYTES, "total_chunk_count": chunks},
        })
        try:
            return str(data["publish_id"]), str(data["upload_url"])
        except KeyError as exc:
            raise TransientError("tiktok file-upload init malformed") from exc

    def _upload_chunks(self, ctx: PublishContext, upload_url: str, total: int) -> None:
        offset = 0
        with open(ctx.clip_path, "rb") as handle:
            while offset < total:
                ctx.check_cancelled()
                chunk = handle.read(CHUNK_BYTES)
                if not chunk:
                    break
                end = offset + len(chunk) - 1
                request = urllib.request.Request(
                    upload_url, data=chunk, method="PUT",
                    headers={"Content-Type": "video/mp4",
                             "Content-Length": str(len(chunk)),
                             "Content-Range": f"bytes {offset}-{end}/{total}"})
                try:
                    with urllib.request.urlopen(request, timeout=300):
                        pass
                except urllib.error.HTTPError as exc:
                    if exc.code in (500, 502, 503, 504):
                        raise TransientError("tiktok chunk failed, retry job") from exc
                    try:
                        detail = exc.read().decode("utf-8", "replace")[:300]
                    except Exception:
                        detail = ""
                    raise classify_http_status(exc.code, detail) from exc
                except OSError as exc:
                    raise TransientError(f"tiktok chunk unreachable: {exc}") from exc
                offset += len(chunk)
                ctx.on_progress(min(1.0, offset / total), "uploading")

    def _finish_direct_post(self, token: str, publish_id: str,
                            request) -> str:
        data = self._api("POST", "/post/publish/", token, {
            "post_info": {"title": (request.caption or request.title)[:2200],
                          "privacy_level": _privacy_level(request.privacy)},
            "source_info": {"source": "FILE_UPLOAD", "publish_id": publish_id},
            "post_mode": "DIRECT_POST",
        })
        return str(data.get("publish_id", publish_id))

    def _finish_draft(self, token: str, publish_id: str, request) -> str:
        data = self._api("POST", "/post/publish/", token, {
            "post_info": {"title": (request.caption or request.title)[:2200],
                          "privacy_level": "SELF_ONLY"},
            "source_info": {"source": "FILE_UPLOAD", "publish_id": publish_id},
            "post_mode": "MEDIA_UPLOAD",
        })
        return str(data.get("publish_id", publish_id))


def _privacy_level(privacy: str) -> str:
    return {"public": "PUBLIC_TO_EVERYONE", "private": "SELF_ONLY",
            "unlisted": "SELF_ONLY"}.get(privacy, "SELF_ONLY")


def _raise_tiktok_error(data: dict) -> None:
    """TikTok wraps failures as 200 + error.code. Map honestly."""
    error = data.get("error") or {}
    code = str(error.get("code", "ok"))
    if code.lower() in ("ok", "0", ""):
        return
    message = str(error.get("message", ""))[:300]
    if "scope" in message.lower() or "unauthorized" in message.lower():
        raise ApprovalRequiredError(f"tiktok scope rejected: {message}")
    raise SocialError(f"tiktok api error {code}: {message}",
                      code="tiktok_error",
                      user_message="TikTok menolak permintaan.")
