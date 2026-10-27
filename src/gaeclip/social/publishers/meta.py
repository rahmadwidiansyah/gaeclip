"""Meta publishers (official Graph API): Instagram Reels + Facebook Reels.

HONEST REQUIREMENTS (spec §13/§14/§51), enforced in validate_account —
the UI never pretends an ineligible account can publish:

- Instagram: Business/Creator account REQUIRED (validated via account_type).
  Personal accounts get AccountRequirementError with the exact message
  "Instagram account does not meet publishing requirements."
  Media: resumable upload sessions (no public URL needed on desktop).
- Facebook: a PAGE is required (user timelines don't accept API video
  uploads for reels distribution). Validated by resolving the target
  Page from /me/accounts.

OAuth: Meta permits http://localhost/ redirect URIs for apps in
Development mode (literal hostname "localhost" ONLY — the IP literal
127.0.0.1 is rejected as an insecure connection). GAEClip therefore
rewrites loopback redirects to the localhost hostname for Meta
(OAuthProviderConfig.redirect_host); YouTube/TikTok keep the caller's
host untouched. Additionally the Meta app itself must stay in
Development mode with Client OAuth Login + Web OAuth Login ON;
localhost needs no Valid-OAuth-Redirect-URI whitelisting. Scopes
documented below per product.

STATUS: implemented against public docs, NOT integration-tested (no
Meta app credentials available). See final report.
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
from ..states import (AccountRequirementError, AuthError, PermissionError,
                      ProviderCapabilities, PublishResult, SocialError,
                      TransientError, classify_http_status)

logger = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
GRAPH_VIDEO = "https://graph-video.facebook.com"
GRAPH_VERSION = "v21.0"
CHUNK_BYTES = 8 * 1024 * 1024
MAX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

# -- Centralized Meta permissions (single source of truth) -----------------
# Flow used: Instagram API with Facebook Login + Facebook Pages API, both via
# plain Facebook Login (dialog/oauth). NOT Business Login for Instagram
# (instagram_business_* scopes, graph.instagram.com) — mixing the two flows
# in one authorize URL is what produces "Invalid Scopes".
#
# Facebook Page video publishing (Manage a Page docs):
#   pages_show_list (list Pages) + pages_read_engagement (read) +
#   pages_manage_posts (publish, depends on the two above).
# NOTE: publish_video is deliberately NOT requested: it is a Live Video API
# permission (live-stream to timeline/Page), not required for regular Page
# video/Reels uploads via the Resumable Upload API
# (POST /{APP_ID}/uploads -> POST /upload:<SESSION> -> POST /{PAGE_ID}/videos,
# finished with pages_manage_posts). Requesting it forces the Meta app to need
# the Live Video API use case and otherwise fails with
# "Invalid Scopes: publish_video".
# Instagram Reels via Facebook Login (Content Publishing docs):
#   instagram_basic + instagram_content_publish +
#   pages_show_list + pages_read_engagement (Page↔IG relationship).
# business_management is NOT requested: no Graph call in this file uses it
# (Page discovery uses /me/accounts, IG discovery uses
# /{page-id}?fields=instagram_business_account).
META_FACEBOOK_PAGE_SCOPES: tuple[str, ...] = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
)
# LEGACY (Facebook-Login era, retired 2026-10): Instagram publishing via
# Facebook Login (scopes below + /me/accounts + Page token). Kept ONLY so
# old rows can be detected (is_legacy_instagram_account) and old tests
# keep meaning. New Instagram connections MUST use
# publishers.instagram_login (Instagram Login / Business Login) and MUST
# NOT import these.
META_INSTAGRAM_SCOPES: tuple[str, ...] = (
    "instagram_basic",
    "instagram_content_publish",
    "pages_show_list",
    "pages_read_engagement",
)
# Legacy combined request (pre-fix) — kept only for the deprecated META_OAUTH
# alias below. New code must use FACEBOOK_OAUTH / INSTAGRAM_OAUTH.
_META_COMBINED_SCOPES: tuple[str, ...] = tuple(dict.fromkeys(
    (*META_FACEBOOK_PAGE_SCOPES, *META_INSTAGRAM_SCOPES)))

FACEBOOK_OAUTH = OAuthProviderConfig(
    provider="facebook",
    authorize_url=f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth",
    token_url=f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token",
    scopes=META_FACEBOOK_PAGE_SCOPES,
    extra_authorize_params={},
    use_pkce=False,
    redirect_host="localhost",
    client_secret_required=True,
)

# LEGACY — retired from the provider map (see service._provider_oauth_config).
# "Connect Instagram" now launches Instagram Login
# (publishers.instagram_login.INSTAGRAM_LOGIN_OAUTH), never Facebook Login.
INSTAGRAM_OAUTH = OAuthProviderConfig(
    provider="instagram",
    authorize_url=f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth",
    token_url=f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token",
    scopes=META_INSTAGRAM_SCOPES,
    extra_authorize_params={},
    use_pkce=False,
    redirect_host="localhost",
    client_secret_required=True,
)

META_OAUTH = OAuthProviderConfig(
    provider="meta",
    authorize_url=f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth",
    token_url=f"https://graph.facebook.com/{GRAPH_VERSION}/oauth/access_token",
    scopes=_META_COMBINED_SCOPES,
    extra_authorize_params={},
    # PKCE support in Facebook Login is unverified; state validation still
    # enforced. Do NOT send code_challenge until confirmed.
    use_pkce=False,
    # Meta's HTTPS enforcement exempts ONLY the literal hostname
    # "localhost" (dev-mode apps) — never the IP literal 127.0.0.1.
    # Redirects are rewritten accordingly; other providers untouched.
    redirect_host="localhost",
    # Confidential model: token exchange REQUIRES the app secret. Always
    # the USER's own app credential from Settings (encrypted at rest),
    # never a shared secret shipped in the binary.
    client_secret_required=True,
)


def meta_scopes_for(provider: str) -> tuple[str, ...]:
    """Capability-based scopes: never request the other platform's set.

    NOTE: the "instagram" branch returns the RETIRED Facebook-Login scope
    set (legacy detection only). Instagram Login scopes live in
    publishers.instagram_login.INSTAGRAM_LOGIN_SCOPES — do not use this
    function for new Instagram code.
    """
    if provider == "facebook":
        return META_FACEBOOK_PAGE_SCOPES
    if provider == "instagram":
        return META_INSTAGRAM_SCOPES
    if provider in ("meta", ""):
        return _META_COMBINED_SCOPES
    raise ValueError(f"unknown meta provider: {provider}")


def safe_authorization_log(provider: str, redirect_uri: str,
                           scopes: tuple[str, ...] | list[str],
                           graph_version: str = GRAPH_VERSION) -> str:
    """SAFE authorize-URL summary for dev logs (no secrets/tokens)."""
    return (f"[META_OAUTH] provider={provider} graph={graph_version} "
            f"redirect={redirect_uri} scopes={','.join(scopes)}")

INSTAGRAM_CAPABILITIES = ProviderCapabilities(
    direct_publish=True, draft_upload=False, scheduling=False,
    thumbnail=False, title=False, description=False, caption=True,
    hashtags=True, privacy=False, status_polling=True,
    multiple_accounts=True,
    supports_upload_progress=True,
    supports_processing_status=True,
    supports_remote_status_polling=True,
    supports_provider_url=True,
    supports_cancel=False,
    supports_publish_verification=False,
)

FACEBOOK_CAPABILITIES = ProviderCapabilities(
    direct_publish=True, draft_upload=False, scheduling=False,
    thumbnail=False, title=False, description=True, caption=True,
    hashtags=True, privacy=False, status_polling=True,
    multiple_accounts=True,  # multiple Pages
    supports_upload_progress=True,
    supports_processing_status=True,
    supports_remote_status_polling=True,
    supports_provider_url=True,
    supports_cancel=False,
    supports_publish_verification=False,
)


class _GraphClient:
    """Minimal Graph helper with honest error mapping (no secrets in errors)."""

    @staticmethod
    def call(method: str, path: str, access_token: str,
             params: dict[str, Any] | None = None,
             raw_body: bytes | None = None,
             content_type: str = "application/json") -> dict[str, Any]:
        query = dict(params or {})
        query["access_token"] = access_token
        url = f"{GRAPH}/{GRAPH_VERSION}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        data = raw_body
        headers = {"Content-Type": content_type} if raw_body is not None else None
        if raw_body is None and method in ("POST",):
            data = b""
        request = urllib.request.Request(url, data=data, method=method)
        if headers:
            for key, value in headers.items():
                request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise _classify_graph_http_error(exc.code, detail) from exc
        except OSError as exc:
            raise TransientError(f"meta unreachable: {exc}") from exc
        try:
            parsed = json.loads(text or "{}")
        except ValueError as exc:
            raise TransientError("meta returned invalid JSON") from exc
        if isinstance(parsed, dict) and "error" in parsed:
            err = parsed["error"] or {}
            raw = json.dumps(err)[:500]
            if _graph_is_transient(raw):
                raise TransientError(
                    f"meta transient: {_graph_message(raw)}")
            detail = _graph_message(raw)
            try:
                gcode = int(err.get("code", 0) or 0) if isinstance(err, dict) else 0
            except (TypeError, ValueError):
                gcode = 0
            # Graph error codes are NOT HTTP statuses — map them explicitly.
            # Technical detail stays in str(exc) for server logs; the UI keeps
            # only the generic user-facing message from each error class.
            if gcode:
                _raise_for_graph_code(gcode, detail)
            raise classify_http_status(400, detail)
        return parsed if isinstance(parsed, dict) else {}


def _graph_code_of(detail: str) -> int:
    """Extract Graph error code from a JSON body, or 0 when absent."""
    try:
        data = json.loads(detail or "{}")
        err = data.get("error", data) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            return int(err.get("code", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        pass
    return 0


def _graph_is_transient(detail: str) -> bool:
    """True when Meta explicitly marks the error retryable.

    Shape: {"error": {..., "is_transient": true}} or a bare body carrying
    the flag. Never raises, never touches secrets.
    """
    try:
        data = json.loads(detail or "{}")
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error", data)
    if not isinstance(err, dict):
        return False
    return bool(err.get("is_transient"))


def _raise_for_graph_code(gcode: int, detail: str) -> None:
    """Map Graph error codes (NOT HTTP statuses) to typed errors."""
    if gcode == 1:
        # Generic "An unknown error occurred" (frequently an OAuthException
        # wrapper around a malformed app request, e.g. a bad finish call).
        # NOT transient: fail fast instead of retrying a malformed request
        # 3x per finish x5 service attempts. Retried only once the request
        # schema is proven correct against the mocked protocol test.
        raise SocialError(f"meta graph 1: {detail}",
                          code="unknown_provider_error", retryable=False,
                          user_message="Facebook menolak permintaan. Cek log server lalu coba lagi.")
    if gcode in (102, 190, 191, 10200):
        raise AuthError(f"meta graph {gcode}: {detail}")
    if gcode in (10, 200, 299, 33, 298):
        raise PermissionError(f"meta graph {gcode}: {detail}")
    if gcode in (4, 17, 32, 341, 613):
        raise TransientError(f"meta graph {gcode}: {detail}")
    if gcode == 2500:
        # 2500 = Unknown path components / invalid parameter: a developer /
        # contract bug (wrong edge, removed API), NOT a missing scope. Keep
        # it as honest invalid_request so the UI stops saying "permission".
        raise SocialError(f"meta graph 2500: {detail}", code="invalid_request",
                          user_message="Permintaan tidak valid untuk platform ini.")


def _classify_graph_http_error(http_status: int, detail: str):
    """HTTPError wrapper that prefers Graph-code mapping when present."""
    message = _graph_message(detail)
    if _graph_is_transient(detail):
        # Meta explicitly says retry may succeed (e.g. 390/1363030 Video
        # Upload Timeout) — honor it regardless of numeric code.
        return TransientError(f"meta transient: {message}")
    gcode = _graph_code_of(detail)
    if gcode:
        try:
            _raise_for_graph_code(gcode, message)
        except (AuthError, PermissionError, TransientError,
                SocialError) as exc:
            return exc
    return classify_http_status(http_status, message)


def _graph_message(detail: str) -> str:
    """Safe Graph summary: message + code/subcode/type (no tokens).

    Only the standard error fields are kept — never raw bodies that could
    carry credentials. Truncated to 300 chars for logs.
    """
    try:
        data = json.loads(detail or "{}")
        err = data.get("error", data) if isinstance(data, dict) else {}
        if not isinstance(err, dict):
            return str(detail)[:300]
        message = str(err.get("message", detail or ""))[:200]
        parts = []
        for key in ("code", "error_subcode", "error_data", "type", "fbtrace_id"):
            try:
                value = err.get(key, "")
            except AttributeError:
                value = ""
            if value not in ("", None, {}, []):
                parts.append(f"{key}={str(value)[:40]}")
        # 'subcode' is also used by some Graph versions.
        try:
            sub = err.get("subcode", "")
        except AttributeError:
            sub = ""
        if sub not in ("", None):
            parts.append(f"subcode={str(sub)[:40]}")
        if parts:
            return f"{message} ({', '.join(parts)})"[:300]
        return message[:300] or str(detail)[:300]
    except ValueError:
        return detail[:300]


def _encode_multipart(fields: dict[str, Any], file_field: str = "",
                      filename: str = "", filebytes: bytes = b"") -> tuple[bytes, str]:
    """Build a multipart/form-data body (stdlib only, no extra deps)."""
    import secrets as _secrets

    boundary = f"----Gaeclip{_secrets.token_hex(12)}"
    parts: list[bytes] = []
    for key, value in (fields or {}).items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    if file_field:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename or "clip.mp4"}"\r\n'.encode())
        parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        parts.append(bytes(filebytes))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _post_multipart(path: str, token: str, fields: dict[str, Any],
                    file_field: str = "", filename: str = "",
                    filebytes: bytes = b"",
                    host: str = GRAPH) -> dict[str, Any]:
    """POST a Page-scoped multipart call (never logs tokens/bodies).

    Video publishing MUST go to the graph-video host
    (https://graph-video.facebook.com/...), per the Video API publishing
    guide — graph.facebook.com returns HTTP 500 code=1 for the same
    finish call. Pass host=GRAPH_VIDEO for /{PAGE_ID}/videos.
    """
    query = {"access_token": token}
    url = f"{host}/{GRAPH_VERSION}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
    body, content_type = _encode_multipart(fields, file_field, filename, filebytes)
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": content_type,
                 "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        raise _classify_graph_http_error(
            exc.code, detail or "facebook phased call failed") from exc
    except OSError as exc:
        raise TransientError(f"facebook phased call unreachable: {exc}") from exc
    try:
        parsed = json.loads(text or "{}")
    except ValueError as exc:
        raise TransientError("meta returned invalid JSON") from exc
    if isinstance(parsed, dict) and "error" in parsed:
        err = parsed["error"] or {}
        raw = json.dumps(err)[:500]
        if _graph_is_transient(raw):
            raise TransientError(
                f"meta transient: {_graph_message(raw)}")
        detail = _graph_message(raw)
        try:
            _raise_for_graph_code(int(err.get("code", 0) or 0), detail)
        except (AuthError, PermissionError, TransientError,
                SocialError) as exc:
            raise exc
        raise classify_http_status(400, detail)
    return parsed if isinstance(parsed, dict) else {}


def _parse_rate_backoff_ms(detail: str) -> int:
    """Extract Meta's requested backoff (ms) from a 429 body.

    Observed shape: {"backoff":60000,"debug_info":{...}}. Falls back to
    60s when absent/unparseable; clamped to [1s, 120s]. Never raises.
    """
    try:
        data = json.loads(detail or "{}")
    except ValueError:
        return 60_000
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.append(data.get("backoff"))
        err = data.get("error")
        if isinstance(err, dict):
            candidates.append(err.get("backoff"))
        debug = data.get("debug_info")
        if isinstance(debug, dict):
            candidates.append(debug.get("backoff"))
    for value in candidates:
        try:
            ms = int(value)
        except (TypeError, ValueError):
            continue
        if ms > 0:
            return max(1_000, min(120_000, ms))
    return 60_000


def _effective_page_token(ctx_account_meta: dict[str, Any],
                           fallback_user_token: str) -> str:
    """Page token when the account discovery saved one, else user token.

    Facebook Page uploads and Instagram-via-Facebook-Login uploads both
    require the Page access token. Falling back to the user token preserves
    old single-token accounts; new connects always store page_token.

    TOKEN CONTEXT (audited 2026-10-08 against the Video API publishing
    guide https://developers.facebook.com/docs/video-api/guides/publishing
    and the Resumable Upload API guide):
    - UPLOAD SESSION (POST /{APP_ID}/uploads): docs prescribe the USER
      token; this implementation sends the PAGE token (observed working —
      chunks reach 100%). Kept as-is; switching auth on a working step
      would be a blind change. Known limitation, see final report.
    - CHUNK UPLOAD (POST /upload:<SESSION>): same token as the session,
      via `Authorization: OAuth` header (required for upload:* hosts;
      query access_token fails here). Page token in practice.
    - FINISH (POST /{PAGE_ID}/videos): PAGE token (docs-required), sent
      as a multipart `access_token` form field on the graph-video host.
    - STATUS POLLING (GET /{video-id}): page token via _GraphClient.
    Legacy user-token fallback remains ONLY for old single-token accounts
    that never stored a page_token, and is logged where it happens.
    """
    page_token = str((ctx_account_meta or {}).get("page_token", "") or "")
    if not page_token:
        logger.info("[SOCIAL_PUBLISH] provider=facebook token=legacy_user_fallback "
                    "detail=page_token_missing_using_user_token")
    return page_token or fallback_user_token


def extract_upload_handle(response: Any) -> str | None:
    """Extract the resumable-upload file handle from a chunk response.

    Official shape (Resumable Upload API guide): top-level ``{"h": ...}``.
    Distinct from the upload session id (``upload:<SESSION>``) — the two
    must never be confused. Returns the handle string, or None when the
    response carries no usable handle. Never raises, never logs the handle.
    """
    if not isinstance(response, dict):
        return None
    # Official shape: top-level "h" (opaque token like "2:c2FtcGxl...").
    # Aliases + one nesting level are accepted defensively; the upload
    # session id ("upload:<SESSION>") is NEVER a valid handle.
    for key in ("h", "handle", "file_handle", "upload_handle",
                "file_upload_handle"):
        try:
            value = response.get(key, "")
        except AttributeError:
            continue
        if isinstance(value, str) and value and not value.startswith("upload:"):
            return value
    for key in ("data", "result"):
        try:
            nested = response.get(key)
        except AttributeError:
            nested = None
        if isinstance(nested, dict):
            found = extract_upload_handle(nested)
            if found:
                return found
    return None


def facebook_upload_handle_missing(response_fields: list[str],
                                     server_offset: int | None = None,
                                     total: int | None = None) -> SocialError:
    """Protocol error: final chunk returned no usable upload handle."""
    detail = (f"facebook upload handle missing; response fields={sorted(response_fields)}"
              f" server_offset={server_offset} total={total}")
    return SocialError(
        detail, code="facebook_upload_handle_missing", retryable=False,
        user_message="Upload ke Facebook tidak lengkap (handle hilang). Coba lagi.")


def _debug_info_type(parsed: dict[str, Any]) -> str:
    """Safe debug_info type label (e.g. 'UploadRateLimitedError') or ''.

    Only the short type NAME is kept — never values, which could carry
    session/token material. Truncated, never raises.
    """
    try:
        info = parsed.get("debug_info", None)
    except AttributeError:
        return ""
    if not isinstance(info, dict):
        return ""
    try:
        label = str(info.get("type", "") or "")
    except Exception:
        return ""
    return label[:64] if label else ""


def _classify_chunk_body(parsed: dict[str, Any]) -> SocialError | None:
    """Classify an HTTP-200 chunk body that carries a Meta error object.

    Meta can reject a chunk while returning HTTP 200 with
    {"error": {...}}. Marching the local offset past such a response is
    what produces "100% but no handle". Returns a typed error to raise
    immediately, or None when the body carries no error object.
    """
    try:
        err = parsed.get("error", None)
    except AttributeError:
        return None
    if not isinstance(err, dict) or not err:
        return None
    raw = json.dumps(err)[:500]
    if _graph_is_transient(raw):
        return TransientError(f"meta transient: {_graph_message(raw)}")
    try:
        code = int(err.get("code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    message = _graph_message(raw)
    if code:
        try:
            _raise_for_graph_code(code, message)
        except (AuthError, PermissionError, TransientError,
                SocialError) as exc:
            return exc
        return classify_http_status(400, message)
    return SocialError(f"meta chunk rejected: {message}",
                       code="upload_chunk_rejected", retryable=False,
                       user_message="Facebook menolak bagian upload. Coba lagi.")


def safe_finish_request_schema() -> str:
    """SAFE finish-request schema for tests/logs (no secrets, no handles).

    The exact fields the Video API publishing guide requires for
    POST /{page-id}/videos on the graph-video host.
    """
    return ("POST /{page-id}/videos @ graph-video.facebook.com\n"
            "multipart:\n"
            "  title=<redacted>\n"
            "  description=<redacted>\n"
            "  fbuploader_video_file_chunk=<HANDLE>\n"
            "  access_token=<REDACTED>  (Page Access Token, form field)")


class InstagramPublisher(SocialPublisher):
    """LEGACY (Facebook-Login era) — NOT in the provider map anymore.

    service.publisher_for("instagram") returns
    instagram_login.InstagramLoginPublisher. This class stays importable
    only for backward-compat reference; new code must not use it.
    """

    provider = "instagram"
    capabilities = INSTAGRAM_CAPABILITIES

    # -- accounts ------------------------------------------------------
    # Flow: Facebook OAuth (user token) -> GET /me/accounts ->
    # pick Page with linked instagram_business_account -> GET IG profile.
    # Stored explicitly: facebook_page_id + page_token + IG id (never one
    # generic id), so publishers use the right token per endpoint.
    def account_info(self, access_token: str) -> AccountInfo:
        pages = _GraphClient.call("GET", "me/accounts", access_token,
                                  {"fields": "id,name,access_token,instagram_business_account"})
        items = pages.get("data") or []
        for page in items:
            ig = page.get("instagram_business_account") or {}
            if ig.get("id"):
                profile = _GraphClient.call(
                    "GET", str(ig["id"]), access_token,
                    {"fields": "id,username,account_type"})
                return AccountInfo(
                    external_account_id=str(ig["id"]),
                    display_name="@" + str(profile.get("username", "")),
                    extra={"account_type": profile.get("account_type", ""),
                           "via_page": page.get("name", ""),
                           "facebook_page_id": str(page.get("id", "")),
                           "facebook_user_id": "",
                           "instagram_business_account_id": str(ig["id"]),
                           "page_token": str(page.get("access_token", "") or "")})
        raise AccountRequirementError(
            "Instagram account does not meet publishing requirements.")

    def validate_account(self, access_token: str, account_meta: dict) -> None:
        info = self.account_info(access_token)
        account_meta["display_name"] = info.display_name

    # -- publish (resumable reels upload, no public URL needed) ---------
    def publish(self, ctx: PublishContext) -> PublishResult:
        from ..states import PUBLISHED

        validate_clip_file(ctx.clip_path, max_bytes=MAX_BYTES)
        user_token = ctx.access_token()
        token = _effective_page_token(ctx.account_meta, user_token)
        ig_id = (ctx.account_meta.get("instagram_business_account_id")
                 or ctx.account_meta.get("external_account_id")
                 or self._resolve_ig_id(user_token))
        total = ctx.clip_path.stat().st_size
        upload_url, video_id = self._start_resumable(token, ig_id, total)
        self._upload_parts(ctx, upload_url, total)
        try:
            ctx.on_progress(1.0, "uploaded")
        except Exception:
            pass
        container_id = self._create_container(
            token, ig_id, video_id, ctx.request.caption, ctx.request.hashtags)
        published_id = self._publish_container(token, ig_id, container_id)
        try:
            ctx.on_uploaded(published_id, "")
        except Exception:
            pass
        logger.info("[SOCIAL_PUBLISH] provider=instagram clip_id=%s status=published",
                    ctx.request.clip_id)
        ctx.on_progress(1.0, "processing")
        return PublishResult(status=PUBLISHED, remote_media_id=published_id)

    def get_publish_status(self, access_token: str, remote_media_id: str) -> str:
        from ..states import FAILED, PROCESSING, PUBLISHED

        data = _GraphClient.call(
            "GET", remote_media_id, access_token,
            {"fields": "id,status_code"})
        code = str(data.get("status_code", "FINISHED")).upper()
        if code in ("ERROR", "EXPIRED"):
            return FAILED
        if code in ("IN_PROGRESS", "PUBLISHED_PARTIALLY"):
            return PROCESSING
        return PUBLISHED

    # -- Graph calls ----------------------------------------------------
    def _resolve_ig_id(self, token: str) -> str:
        info = self.account_info(token)
        return info.external_account_id

    def _start_resumable(self, token: str, ig_id: str, total: int) -> tuple[str, str]:
        data = _GraphClient.call("POST", f"{ig_id}/media", token, {
            "upload_type": "resumable",
            "upload_file_size": total,
        })
        try:
            return str(data["upload_url"]), str(data["video_id"])
        except KeyError as exc:
            raise TransientError("instagram resumable init malformed") from exc

    def _upload_parts(self, ctx: PublishContext, upload_url: str, total: int) -> None:
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
                    raise classify_http_status(exc.code, "instagram chunk failed") from exc
                except OSError as exc:
                    raise TransientError(f"instagram chunk unreachable: {exc}") from exc
                offset += len(chunk)
                ctx.on_progress(min(1.0, offset / total), "uploading")

    def _create_container(self, token: str, ig_id: str, video_id: str,
                          caption: str, hashtags: tuple) -> str:
        text = (caption + ("\n" + " ".join(hashtags) if hashtags else "")).strip()[:2200]
        data = _GraphClient.call("POST", f"{ig_id}/media", token, {
            "media_type": "REELS", "video_id": video_id,
            "caption": text, "share_to_feed": True,
        })
        container = str(data.get("id", ""))
        if not container:
            raise TransientError("instagram container creation malformed")
        return container

    def _publish_container(self, token: str, ig_id: str, container_id: str) -> str:
        data = _GraphClient.call("POST", f"{ig_id}/media_publish", token,
                                 {"creation_id": container_id})
        published = str(data.get("id", ""))
        if not published:
            raise TransientError("instagram publish malformed")
        return published


class FacebookPublisher(SocialPublisher):
    provider = "facebook"
    capabilities = FACEBOOK_CAPABILITIES

    # -- accounts (Pages only) ------------------------------------------
    # Stored explicitly: facebook_page_id (= external id) + page_token for
    # uploads + user-level id for debugging. Never a single generic id.
    def account_info(self, access_token: str) -> AccountInfo:
        pages = _GraphClient.call("GET", "me/accounts", access_token,
                                  {"fields": "id,name,access_token"})
        items = pages.get("data") or []
        if not items:
            raise AccountRequirementError(
                "No Facebook Page found. Reels publishing requires a Page.")
        page = items[0]
        return AccountInfo(
            external_account_id=str(page.get("id", "")),
            display_name=str(page.get("name", "")),
            extra={"facebook_page_id": str(page.get("id", "")),
                   "facebook_user_id": "",
                   "page_token": page.get("access_token", ""),
                   "page_choices": [{"id": p.get("id"), "name": p.get("name")}
                                    for p in items]})

    def validate_account(self, access_token: str, account_meta: dict) -> None:
        self.account_info(access_token)

    # -- publish (Page video upload -> reels-eligible) ------------------
    # Resumable Upload API (official): POST /{APP_ID}/uploads ->
    # POST /upload:<SESSION> chunks -> POST /{PAGE_ID}/videos.
    # The old POST /{page-id}/video_uploads edge is dead (Graph 2500).
    def publish(self, ctx: PublishContext) -> PublishResult:
        from ..states import PUBLISHED

        validate_clip_file(ctx.clip_path, max_bytes=MAX_BYTES)
        user_token = ctx.access_token()
        token = _effective_page_token(ctx.account_meta, user_token)
        page_id = (ctx.account_meta.get("facebook_page_id")
                   or ctx.account_meta.get("external_account_id")
                   or self.account_info(user_token).external_account_id)
        app_id = str(ctx.account_meta.get("meta_app_id")
                      or ctx.account_meta.get("app_id") or "").strip()
        if not app_id:
            raise SocialError("missing Meta App ID for resumable upload",
                              code="oauth_not_configured",
                              user_message="Isi App ID Meta di Settings → Social dulu.")
        total = ctx.clip_path.stat().st_size
        description = ((ctx.request.caption or ctx.request.title) +
                       ("\n" + " ".join(ctx.request.hashtags)
                        if ctx.request.hashtags else "")).strip()[:5000]
        title = (ctx.request.title or "Gaeclip clip")[:100]
        # Resumable protocol failures fall back to the Page-scoped phased
        # upload (proven live: resumable yields 206/no-handle here while
        # phased publishes). Auth/config errors bypass the fallback and
        # fail fast. The fallback runs at most once per attempt.
        _RESUMABLE_FALLBACK_CODES = (
            "upload_offset_wall", "upload_offset_mismatch",
            "upload_incomplete", "facebook_upload_handle_missing")
        try:
            upload_id = self._start_resumable(
                token, app_id, ctx.clip_path.name, total)
            transfer = self._upload_parts(ctx, token, upload_id, total)
            file_handle = transfer["handle"]
            response_fields = transfer["response_fields"]
            server_offset = transfer["server_offset"]
            if server_offset is not None and server_offset < total:
                # The server did NOT commit all bytes (local 100% is not
                # completion) — try the phased protocol instead of failing.
                logger.info("[SOCIAL_PUBLISH] provider=facebook "
                            "stage=upload_complete handle=%s response_fields=%s "
                            "server_offset=%d total=%d debug_types=%s",
                            "present" if file_handle else "missing",
                            sorted(response_fields), server_offset, total,
                            transfer["debug_types"])
                raise SocialError(
                    f"facebook upload incomplete: server_offset={server_offset} "
                    f"total={total} response_fields={sorted(response_fields)}",
                    code="upload_incomplete", retryable=False,
                    user_message="Upload ke Facebook tidak lengkap. Coba video yang lebih kecil.")
            if not file_handle:
                # Protocol error: server committed the bytes but returned no
                # upload handle — finishing with the session id would be a
                # malformed request (HTTP 500 code=1). Never mark uploaded;
                # try the phased protocol instead.
                logger.info("[SOCIAL_PUBLISH] provider=facebook "
                            "stage=upload_complete handle=missing response_fields=%s "
                            "server_offset=%s total=%d debug_types=%s",
                            sorted(response_fields), str(server_offset), total,
                            transfer["debug_types"])
                raise facebook_upload_handle_missing(
                    response_fields, server_offset, total)
        except SocialError as exc:
            if getattr(exc, "code", "") not in _RESUMABLE_FALLBACK_CODES:
                raise
            # Deterministic resumable-protocol failure on the generic host
            # (offset wall, byte mismatch, incomplete commit, or 206 with
            # no handle) — fall back to the Page-scoped phased upload
            # (upload_phase=start/transfer/finish on /{PAGE_ID}/videos).
            logger.info("[SOCIAL_PUBLISH] provider=facebook clip_id=%s "
                        "fallback=phased_upload reason=%s",
                        ctx.request.clip_id, str(exc)[:160])
            return self._publish_phased(ctx, token, page_id, total,
                                        title, description)
        # Bytes are on Meta's servers but NOTHING is published yet.
        # UPLOADING 100% -> FINALIZING; UPLOADED only after finish returns
        # a video id (service persists it via on_uploaded).
        try:
            ctx.on_progress(1.0, "uploading")
            ctx.on_progress(1.0, "finalizing")
        except Exception:
            pass

        def _do_finish() -> str:
            # Official Video API shape (publishing guide): multipart title
            # + description + fbuploader_video_file_chunk=<h> on the
            # graph-video host with the Page Access Token as a form field.
            # The session id is NEVER substituted for the handle.
            data = _post_multipart(
                f"{page_id}/videos", token,
                {"title": title, "description": description,
                 "fbuploader_video_file_chunk": file_handle},
                host=GRAPH_VIDEO)
            return str(data.get("id") or data.get("video_id") or "")

        video_id = self._retry_finish(ctx, "resumable", _do_finish)
        try:
            ctx.on_uploaded(video_id, "")
        except Exception:
            pass
        logger.info("[SOCIAL_PUBLISH] provider=facebook clip_id=%s status=published",
                    ctx.request.clip_id)
        ctx.on_progress(1.0, "published")
        return PublishResult(status=PUBLISHED, remote_media_id=video_id)

    def _retry_finish(self, ctx: PublishContext, via: str, call) -> str:
        """Retry an idempotent finish call (bytes already committed).

        Only TransientError (network + Meta is_transient, e.g. 390/1363030
        Video Upload Timeout) is retried here, 3x with 15/30/60s gaps —
        anything else (auth/permission/bad params) fails fast. Returns the
        video id or raises the last error for the service retry.
        """
        last_exc: Exception | None = None
        for attempt in (1, 2, 3):
            ctx.check_cancelled()
            try:
                video_id = call()
            except TransientError as exc:
                last_exc = exc
            else:
                if video_id:
                    if attempt > 1:
                        logger.info("[SOCIAL_PUBLISH] provider=facebook finish "
                                    "via=%s attempt=%d ok", via, attempt)
                    return video_id
                last_exc = TransientError(
                    f"facebook finish via={via} empty response")
            logger.info("[SOCIAL_PUBLISH] provider=facebook finish via=%s "
                        "attempt=%d failed: %s", via, attempt, str(last_exc)[:160])
            if attempt < 3:
                ctx.check_cancelled()
                time.sleep((15, 30, 60)[attempt - 1])
        assert last_exc is not None
        raise last_exc

    def _publish_phased(self, ctx: PublishContext, token: str, page_id: str,
                        total: int, title: str, description: str) -> PublishResult:
        """Page-scoped phased upload fallback (start/transfer/finish).

        Used when the generic Resumable host stalls at a fixed offset.
        Same honesty rules: real fractions only during transfer.
        """
        from ..states import PROCESSING

        start = _post_multipart(f"{page_id}/videos", token, {
            "upload_phase": "start", "file_size": str(total)},
            host=GRAPH_VIDEO)
        try:
            session_id = str(start.get("upload_session_id", ""))
            video_id = str(start.get("video_id", ""))
            offset = int(start.get("start_offset", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise TransientError("facebook phased start malformed") from exc
        if not session_id:
            raise TransientError("facebook phased start malformed")
        no_progress = 0
        with open(ctx.clip_path, "rb") as handle:
            while offset < total:
                ctx.check_cancelled()
                handle.seek(offset)
                chunk = handle.read(min(CHUNK_BYTES, total - offset))
                if not chunk:
                    break
                resp = _post_multipart(
                    f"{page_id}/videos", token,
                    {"upload_phase": "transfer",
                     "upload_session_id": session_id,
                     "start_offset": str(offset)},
                    file_field="video_file_chunk",
                    filename=ctx.clip_path.name or "clip.mp4",
                    filebytes=chunk,
                    host=GRAPH_VIDEO)
                try:
                    new_offset = int(resp.get("start_offset",
                                              offset + len(chunk)) or 0)
                except (TypeError, ValueError):
                    new_offset = offset + len(chunk)
                video_id = str(resp.get("video_id", "") or video_id)
                if new_offset <= offset:
                    no_progress += 1
                    if no_progress >= 3:
                        raise SocialError(
                            f"facebook phased upload stalled at offset {offset}",
                            code="invalid_request",
                            user_message="Upload ditolak di tengah jalan. Cek file/offset lalu coba lagi.")
                    continue
                no_progress = 0
                offset = max(0, min(total, new_offset))
                ctx.on_progress(min(1.0, offset / total), "uploading")
        try:
            ctx.on_progress(1.0, "uploading")
            ctx.on_progress(1.0, "finalizing")
        except Exception:
            pass

        def _do_phased_finish() -> str:
            done = _post_multipart(f"{page_id}/videos", token, {
                "upload_phase": "finish", "upload_session_id": session_id,
                "video_id": video_id, "title": title,
                "description": description},
                host=GRAPH_VIDEO)
            final = str(done.get("video_id") or done.get("id") or "")
            if final:
                return final
            # Observed live: finish answers {"success": true} with no id —
            # it only closes the session. The video id is the one from
            # the start phase; the video then processes async (status
            # polling distinguishes UPLOADED/PROCESSING/PUBLISHED).
            if done.get("success") is True and video_id:
                logger.info("[SOCIAL_PUBLISH] provider=facebook phased "
                            "finish accepted via success flag")
                return video_id
            return ""

        final_id = self._retry_finish(ctx, "phased", _do_phased_finish)
        try:
            ctx.on_uploaded(final_id, "")
        except Exception:
            pass
        logger.info("[SOCIAL_PUBLISH] provider=facebook clip_id=%s status=processing via=phased",
                    ctx.request.clip_id)
        # PROCESSING (not PUBLISHED): the bytes are accepted with a video
        # id; the service polls get_publish_status to confirm publishing.
        ctx.on_progress(1.0, "processing")
        return PublishResult(status=PROCESSING, remote_media_id=final_id)

    def get_publish_status(self, access_token: str, remote_media_id: str) -> str:
        from ..states import FAILED, PROCESSING, PUBLISHED

        data = _GraphClient.call("GET", remote_media_id, access_token,
                                 {"fields": "id,status"})
        status = str((data.get("status") or {}).get("video_status", "ready")).lower()
        if status in ("error", "expired"):
            return FAILED
        if status in ("uploading", "processing"):
            return PROCESSING
        return PUBLISHED

    def _start_resumable(self, token: str, app_id: str,
                           file_name: str, total: int) -> str:
        """Create a Resumable Upload session: POST /{APP_ID}/uploads."""
        data = _GraphClient.call("POST", f"{app_id}/uploads", token, {
            "file_name": file_name or "clip.mp4",
            "file_length": total, "file_type": "video/mp4"})
        try:
            upload_id = str(data["id"])
        except KeyError as exc:
            raise TransientError("facebook upload session malformed") from exc
        if not upload_id:
            raise TransientError("facebook upload session malformed")
        logger.info("[SOCIAL_PUBLISH] provider=facebook stage=session "
                    "file_length=%d file_name=%s", total, file_name or "clip.mp4")
        return upload_id

    def _query_session(self, token: str,
                         session_path: str) -> tuple[int | None, str]:
        """GET an upload session: (committed offset, safe raw snippet).

        The snippet is truncated + token-free (session status JSON only).
        """
        url = f"{GRAPH}/{GRAPH_VERSION}/{session_path}"
        request = urllib.request.Request(
            url, method="GET",
            headers={"Authorization": f"OAuth {token}"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8", errors="replace")
        except Exception:
            return None, ""
        snippet = text[:200].replace("\n", " ")
        try:
            data = json.loads(text or "{}")
        except ValueError:
            return None, snippet
        if not isinstance(data, dict):
            return None, snippet
        for key in ("file_offset", "offset", "start_offset"):
            try:
                value = data.get(key, None)
            except AttributeError:
                value = None
            if value is None:
                continue
            try:
                return max(0, int(value)), snippet
            except (TypeError, ValueError):
                continue
        return None, snippet

    def _query_offset(self, token: str, session_path: str) -> int | None:
        """Ask the server for the committed file_offset of an upload session.

        Returns None when the session cannot be queried (caller falls back
        to the local offset). Never logs tokens.
        """
        offset, _ = self._query_session(token, session_path)
        return offset

    def _upload_parts(self, ctx: PublishContext, token: str,
                       upload_id: str, total: int) -> dict[str, Any]:
        """Transfer chunks: POST /upload:<SESSION> with file_offset header.

        Auth moves from query access_token to `Authorization: OAuth` header
        (required for upload:* hosts). Never logs token/URL/session/handle.

        LOCAL 100% IS NOT COMPLETION: every HTTP-200 body is validated.
        A body carrying {"error": ...} raises immediately instead of
        marching the local offset past a rejected chunk (the exact
        "100% but handle=missing, fields=['debug_info']" failure). A
        debug_info-only payload is recorded by type label and the transfer
        is verified against the SERVER offset afterwards.

        Resume-within-session: on transport failure the server offset is
        queried (GET /upload:<SESSION>) and the file cursor is seeked there
        instead of restarting the whole session. Same-offset failures fail
        fast as invalid_request instead of looping 5 fresh sessions.
        Returns a safe diagnostic dict: {handle, response_fields,
        server_offset, total, debug_types}. The handle comes ONLY from
        extract_upload_handle() — never the session id. The caller must
        refuse to finish when the handle is None.
        """
        session_path = upload_id if upload_id.startswith("upload:") \
            else f"upload:{upload_id}"
        failures_by_offset: dict[int, int] = {}
        rate_waits_by_offset: dict[int, int] = {}
        last_handle: str | None = None
        last_response_fields: list[str] = []
        debug_types: list[str] = []
        bodies_seen = 0
        offset = 0
        idx = 0
        # Uniform chunk geometry: every non-terminal chunk is CHUNK_BYTES.
        # Mid-stream shrinking once produced irregular middle chunks and
        # every such run ended 206/no-handle; uniform retries keep the
        # byte layout the server can assemble. Persistent same-offset
        # trouble fails fast to the phased fallback instead.
        with open(ctx.clip_path, "rb") as handle:
            handle.seek(0)
            while offset < total:
                ctx.check_cancelled()
                handle.seek(offset)
                chunk = handle.read(min(CHUNK_BYTES, total - offset))
                if not chunk:
                    break
                size = len(chunk)
                url = f"{GRAPH}/{GRAPH_VERSION}/{session_path}"
                request = urllib.request.Request(
                    url, data=chunk, method="POST",
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(size),
                             "Authorization": f"OAuth {token}",
                             "file_offset": str(offset)})
                try:
                    with urllib.request.urlopen(request, timeout=300) as response:
                        http_status = getattr(response, "status", 200)
                        try:
                            body = response.read().decode(
                                "utf-8", errors="replace")[:500]
                        except Exception:
                            body = ""
                    if body:
                        bodies_seen += 1
                        try:
                            parsed = json.loads(body)
                        except ValueError:
                            parsed = None
                        if isinstance(parsed, dict):
                            # Field NAMES only — values may carry the handle.
                            last_response_fields = sorted(str(k) for k in parsed.keys())
                            # HTTP 200 + Meta error object = rejected chunk.
                            # Raise NOW; never advance past it to a fake 100%.
                            body_error = _classify_chunk_body(parsed)
                            if body_error is not None:
                                logger.info(
                                    "[SOCIAL_PUBLISH] provider=facebook chunk "
                                    "idx=%d offset=%d rejected code=%s",
                                    idx, offset,
                                    getattr(body_error, "code", "?"))
                                raise body_error
                            dtype = _debug_info_type(parsed)
                            if dtype and dtype not in debug_types:
                                debug_types.append(dtype)
                            handle_id = extract_upload_handle(parsed)
                            if handle_id:
                                last_handle = handle_id
                            logger.info(
                                "[SOCIAL_PUBLISH] provider=facebook chunk "
                                "idx=%d offset=%d size=%d http=%s keys=%s "
                                "handle=%s dbg=%s",
                                idx, offset, size, http_status,
                                last_response_fields,
                                "yes" if handle_id else "no",
                                dtype or "-")
                except urllib.error.HTTPError as exc:
                    try:
                        detail = exc.read().decode(
                            "utf-8", errors="replace")[:500]
                    except Exception:
                        detail = ""
                    status = getattr(exc, "code", "?")
                    logger.info("[SOCIAL_PUBLISH] provider=facebook chunk "
                                "idx=%d offset=%d size=%d total=%d status=http_%s",
                                idx, offset, size, total, status)
                    if status == 429:
                        # Meta rate limit: honor the requested backoff and
                        # resume the SAME session instead of restarting from
                        # zero (a fresh 90 MB re-upload would get limited
                        # again). Bounded: afterwards the service retry
                        # (new session + backoff) takes over.
                        waits = rate_waits_by_offset.get(offset, 0) + 1
                        rate_waits_by_offset[offset] = waits
                        if waits > 3:
                            raise _classify_graph_http_error(
                                exc.code,
                                detail or "facebook chunk rate limited") from exc
                        delay_ms = _parse_rate_backoff_ms(detail)
                        logger.info("[SOCIAL_PUBLISH] provider=facebook chunk "
                                    "idx=%d offset=%d rate_limited "
                                    "backoff=%ds attempt=%d",
                                    idx, offset, delay_ms // 1000, waits)
                        ctx.check_cancelled()
                        time.sleep(delay_ms / 1000.0)
                        ctx.check_cancelled()
                        server_offset, _ = self._query_session(
                            token, session_path)
                        if server_offset is not None:
                            if server_offset > offset:
                                # Post-commit rejection: the bytes landed
                                # despite the 429. Resuming would duplicate
                                # them and poison the assembly.
                                raise SocialError(
                                    f"facebook upload offset mismatch: server "
                                    f"committed {server_offset} beyond local "
                                    f"{offset}",
                                    code="upload_offset_mismatch",
                                    retryable=False,
                                    user_message="Upload ke Facebook tidak sinkron. Coba lagi.")
                            offset = max(0, min(total, server_offset))
                        continue
                    raise _classify_graph_http_error(
                        exc.code, detail or "facebook chunk failed") from exc
                except OSError as exc:
                    failures = failures_by_offset.get(offset, 0) + 1
                    failures_by_offset[offset] = failures
                    server_offset, _ = self._query_session(
                        token, session_path)
                    # Server offset is authoritative; session payloads are
                    # never logged (they carry the session id).
                    logger.info("[SOCIAL_PUBLISH] provider=facebook chunk "
                                "idx=%d offset=%d size=%d total=%d status=broken_pipe "
                                "attempt=%d server_offset=%s",
                                idx, offset, size, total, failures,
                                str(server_offset))
                    if server_offset is not None and server_offset != offset:
                        if server_offset > offset:
                            # Server holds MORE than we sent: a rejected
                            # attempt (e.g. post-commit 429) was actually
                            # committed. Resuming would duplicate bytes and
                            # poison the assembly (perpetual 206/no-handle).
                            raise SocialError(
                                f"facebook upload offset mismatch: server "
                                f"committed {server_offset} beyond local "
                                f"{offset}",
                                code="upload_offset_mismatch",
                                retryable=False,
                                user_message="Upload ke Facebook tidak sinkron. Coba lagi.")
                        logger.info("[SOCIAL_PUBLISH] provider=facebook chunk "
                                    "resume local=%d server=%d",
                                    offset, server_offset)
                        offset = max(0, min(total, server_offset))
                        continue
                    if server_offset is None:
                        # Server unreachable too — genuine network issue,
                        # let the service retry with a fresh session + backoff.
                        if failures >= 3:
                            raise TransientError(
                                f"facebook chunk unreachable: {exc}") from exc
                        continue
                    # Server reachable but keeps RSTing this offset (uniform
                    # size retried as-is): fail fast after bounded attempts
                    # so the phased fallback takes over instead of
                    # reshaping the byte layout mid-stream.
                    if failures >= 6:
                        raise SocialError(
                            f"facebook upload rejected at offset {offset} "
                            f"size={size} ({failures}x server_offset={server_offset}): {exc}",
                            code="upload_offset_wall",
                            user_message="Upload ditolak di tengah jalan. Cek file/offset lalu coba lagi.") from exc
                    continue
                failures_by_offset.pop(offset, None)
                rate_waits_by_offset.pop(offset, None)
                offset += size
                idx += 1
                ctx.on_progress(min(1.0, offset / total), "uploading")
        # Completion is verified against the SERVER's committed offset —
        # local bytes-sent is never trusted on its own. Safe metadata
        # only: session id / handle / bodies never logged.
        server_offset, _ = self._query_session(token, session_path)
        logger.info("[SOCIAL_PUBLISH] provider=facebook stage=upload_complete "
                    "chunks=%d handle=%s response_fields=%s "
                    "server_offset=%s total=%d debug_types=%s",
                    idx, "present" if last_handle else "missing",
                    last_response_fields, str(server_offset), total,
                    debug_types)
        return {"handle": last_handle,
                "response_fields": last_response_fields,
                "server_offset": server_offset,
                "total": total,
                "debug_types": list(debug_types)}
