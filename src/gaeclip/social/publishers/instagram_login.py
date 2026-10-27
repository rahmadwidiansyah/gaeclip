"""Instagram publisher via Instagram Login (Business Login for Instagram).

Independent OAuth architecture — NO Facebook Login anywhere in this file:

- authorize:  GET https://www.instagram.com/oauth/authorize
- code swap: POST https://api.instagram.com/oauth/access_token (form)
- long-lived: GET https://graph.instagram.com/access_token
- refresh:    GET https://graph.instagram.com/refresh_access_token
- API host:   https://graph.instagram.com (IG User access token)
- publish:    POST /{IG_ID}/media container -> bytes -> status poll
              -> POST /{IG_ID}/media_publish

Official sources (verified 2026-10-08):
- Business Login for Instagram
  (developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login)
- Instagram Platform content publishing guide (Instagram Login column:
  host graph.instagram.com, IG User token, scopes instagram_business_basic
  + instagram_business_content_publish)
- Access Token reference (ig_exchange_token / ig_refresh_token flows)

Requirements: Instagram Professional account (BUSINESS/CREATOR). NO
Facebook Page linkage is required for THIS login setup (official:
"This API setup does not require a Facebook Page to be linked").

KNOWN LIMITATIONS (need a real API test):
- resumable byte upload (upload_type=resumable + rupload host) is
  documented only for Facebook-Login apps. This publisher attempts the
  same container/transfer shape with the IG User token on
  graph.instagram.com; if Meta rejects IG-login tokens there, publish
  fails fast with an honest error (no retry) — see _transfer_bytes.
- redirect URI acceptance (http://localhost loopback) for
  www.instagram.com/oauth/authorize must be confirmed in the App
  Dashboard + real connect test.
- state echo on the Instagram authorize redirect is assumed (OAuth2
  standard); the callback validates it strictly.

Legacy: accounts created through the old Facebook-Login flow carry
page_token/facebook_page_id and NO login_flow marker. They are detected
by is_legacy_instagram_account() and must reconnect — never silently
reinterpreted (see Phase 9).
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

IG_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
IG_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com"
FB_GRAPH = "https://graph.facebook.com"
IG_RUPLOAD = "https://rupload.facebook.com"
IG_GRAPH_VERSION = "v21.0"

MAX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB

# Current Instagram Login scopes (post-2025 names; business_* deprecated
# 2025-01-27). Publishing needs exactly these two; messaging/comments
# scopes are NOT requested (least privilege).
INSTAGRAM_LOGIN_SCOPES: tuple[str, ...] = (
    "instagram_business_basic",
    "instagram_business_content_publish",
)

INSTAGRAM_LOGIN_OAUTH = OAuthProviderConfig(
    provider="instagram",
    authorize_url=IG_AUTHORIZE_URL,
    token_url=IG_TOKEN_URL,
    scopes=INSTAGRAM_LOGIN_SCOPES,
    extra_authorize_params={},
    use_pkce=False,
    redirect_host="localhost",
    client_secret_required=True,
    scope_separator=",",
)

INSTAGRAM_LOGIN_CAPABILITIES = ProviderCapabilities(
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

LOGIN_FLOW_MARKER = "instagram_login"

# Professional account types accepted by Meta (MEDIA_CREATOR is the
# creator subtype returned live 2026-10-08). Unknown values stay
# rejected (fail-closed, never guessed as professional).
PROFESSIONAL_ACCOUNT_TYPES = frozenset({"BUSINESS", "CREATOR", "MEDIA_CREATOR"})


# -- legacy detection ----------------------------------------------------

def is_legacy_instagram_account(account_meta: dict[str, Any] | None) -> bool:
    """True for Facebook-Login-era Instagram credentials.

    Old rows store a Facebook Page token / Page id and no login_flow
    marker. They must reconnect via Instagram Login; never reinterpret.
    """
    meta = account_meta or {}
    if meta.get("login_flow") == LOGIN_FLOW_MARKER:
        return False
    return bool(meta.get("page_token") or meta.get("facebook_page_id"))


def mark_legacy_if_needed(account_meta: dict[str, Any] | None) -> bool:
    """Backfill the LEGACY marker on old rows (metadata only, no delete)."""
    meta = account_meta or {}
    if is_legacy_instagram_account(meta) and not meta.get("legacy_facebook_login"):
        meta["legacy_facebook_login"] = True
        return True
    return False


# -- token flow (Instagram-specific shapes) -------------------------------

def exchange_code_for_short_lived(*, client_id: str, client_secret: str,
                                   redirect_uri: str, code: str) -> dict[str, Any]:
    """Swap the authorize code for a short-lived (~1h) IG User token.

    POST form to api.instagram.com/oauth/access_token. Observed shapes:
    flat {"access_token", "user_id", "permissions"} (seen live 2026-10-08)
    or documented {"data": [{"access_token", "user_id", "permissions"}]}.
    Both accepted. Returns {access_token, ig_user_id, permissions,
    expires_in}.
    """
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    }
    try:
        parsed = _post_form(IG_TOKEN_URL, payload)
    except AuthError as exc:
        raise AuthError(f"instagram code exchange failed: {exc}") from exc
    item: dict[str, Any] = {}
    if isinstance(parsed, dict):
        # Live shape (flat object) takes precedence; documented
        # {"data": [...]} wrapper accepted as fallback.
        if parsed.get("access_token") or parsed.get("user_id"):
            item = parsed
        else:
            try:
                first = (parsed.get("data") or [{}])[0]
                item = first if isinstance(first, dict) else {}
            except (IndexError, AttributeError, TypeError):
                item = {}
    token = str(item.get("access_token", "") or "")
    user_id = str(item.get("user_id", "") or "")
    if not token or not user_id:
        # Safe diagnostic only: key names + standard OAuth error fields
        # (never token/code values) so docker logs reveal Meta's reason.
        raise AuthError(
            "instagram code exchange returned no token/user "
            f"({ _safe_error_summary(parsed)})")
    return {"access_token": token, "ig_user_id": user_id,
            "permissions": str((item or {}).get("permissions", "") or ""),
            "expires_in": 3600}


def exchange_for_long_lived(short_token: str, client_secret: str) -> dict[str, Any]:
    """Short-lived (~1h) -> long-lived (60d) IG User token (server-side)."""
    query = urllib.parse.urlencode({
        "grant_type": "ig_exchange_token",
        "client_secret": client_secret,
        "access_token": short_token,
    })
    try:
        parsed = _get_json(f"{IG_GRAPH}/access_token?{query}")
    except AuthError as exc:
        raise AuthError(f"instagram long-lived exchange failed: {exc}") from exc
    token = str(parsed.get("access_token", "") or "")
    if not token:
        raise AuthError("instagram long-lived exchange returned no token "
                        f"({_safe_error_summary(parsed)})")
    try:
        expires_in = int(parsed.get("expires_in", 5184000) or 5184000)
    except (TypeError, ValueError):
        expires_in = 5184000
    return {"access_token": token, "expires_in": expires_in}


def refresh_long_lived(long_token: str) -> dict[str, Any]:
    """Refresh a valid long-lived token for another 60 days.

    Requirements (official): token unexpired and at least 24h old.
    """
    query = urllib.parse.urlencode({
        "grant_type": "ig_refresh_token",
        "access_token": long_token,
    })
    try:
        parsed = _get_json(f"{IG_GRAPH}/refresh_access_token?{query}")
    except AuthError as exc:
        raise AuthError(f"instagram refresh failed: {exc}") from exc
    token = str(parsed.get("access_token", "") or "")
    if not token:
        raise AuthError("instagram refresh returned no token "
                        f"({_safe_error_summary(parsed)})")
    try:
        expires_in = int(parsed.get("expires_in", 5184000) or 5184000)
    except (TypeError, ValueError):
        expires_in = 5184000
    return {"access_token": token, "expires_in": expires_in}


def _safe_error_summary(parsed: Any) -> str:
    """One-line safe summary of a token-endpoint response without a token.

    Includes key names plus the standard OAuth error fields
    (error/error_type/code/error_message/error_description) — never values
    of token/code/secret fields. Truncated, never raises.
    """
    try:
        if not isinstance(parsed, dict):
            return f"response_type={type(parsed).__name__}"
        keys = sorted(str(k) for k in parsed.keys())[:12]
        parts = [f"keys={keys}"]
        for key in ("error", "error_type", "code", "error_message",
                    "error_description"):
            try:
                value = parsed.get(key, "")
            except AttributeError:
                continue
            if value not in ("", None, {}, []):
                parts.append(f"{key}={str(value)[:160]}")
        data = parsed.get("data")
        if isinstance(data, list):
            parts.append(f"data_len={len(data)}")
            if data and isinstance(data[0], dict):
                parts.append(
                    f"data_keys={sorted(str(k) for k in data[0].keys())[:8]}")
        return " ".join(parts)[:300]
    except Exception:
        return "unparseable"


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise AuthError(f"instagram token endpoint http {exc.code}") from exc
    except OSError as exc:
        raise TransientError(f"instagram token endpoint unreachable: {exc}") from exc
    try:
        parsed = json.loads(body or "{}")
    except ValueError as exc:
        raise AuthError("instagram token endpoint returned invalid JSON") from exc
    return parsed if isinstance(parsed, dict) else {}


def _get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise AuthError(f"instagram token endpoint http {exc.code}") from exc
    except OSError as exc:
        raise TransientError(f"instagram token endpoint unreachable: {exc}") from exc
    try:
        parsed = json.loads(body or "{}")
    except ValueError as exc:
        raise AuthError("instagram token endpoint returned invalid JSON") from exc
    return parsed if isinstance(parsed, dict) else {}


# -- graph.instagram.com client -------------------------------------------

class _IGraphClient:
    """Minimal graph.instagram.com helper (never logs tokens/bodies)."""

    @staticmethod
    def call(method: str, path: str, access_token: str,
             params: dict[str, Any] | None = None,
             host: str = IG_GRAPH) -> dict[str, Any]:
        # Universal Graph shape (used in every official curl example for
        # these endpoints): params — including access_token — as a query
        # string, for GET and POST alike. (A JSON-body variant was tried
        # first; Meta answered code 100 "video_url required", consistent
        # with the body being ignored while auth passed via header.)
        # Never logs tokens/bodies.
        query = dict(params or {})
        query["access_token"] = access_token
        url = (f"{host}/{IG_GRAPH_VERSION}/{path.lstrip('/')}?"
               f"{urllib.parse.urlencode(query)}")
        data = b"" if method == "POST" else None
        request = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            raise _classify_ig_http_error(exc.code, detail) from exc
        except OSError as exc:
            raise TransientError(f"instagram unreachable: {exc}") from exc
        try:
            parsed = json.loads(text or "{}")
        except ValueError as exc:
            raise TransientError("instagram returned invalid JSON") from exc
        if isinstance(parsed, dict) and "error" in parsed:
            err = parsed["error"] or {}
            raw = json.dumps(err)[:500]
            raise _classify_ig_error_dict(err if isinstance(err, dict) else {}, raw)
        return parsed if isinstance(parsed, dict) else {}


def _ig_message(detail: str) -> str:
    try:
        data = json.loads(detail or "{}")
        err = data.get("error", data) if isinstance(data, dict) else {}
        if not isinstance(err, dict):
            return str(detail)[:300]
        message = str(err.get("message", detail or ""))[:200]
        parts = []
        for key in ("code", "error_subcode", "type", "fbtrace_id"):
            try:
                value = err.get(key, "")
            except AttributeError:
                value = ""
            if value not in ("", None, {}, []):
                parts.append(f"{key}={str(value)[:40]}")
        return f"{message} ({', '.join(parts)})"[:300] if parts else message[:300]
    except ValueError:
        return detail[:300]


def _classify_ig_error_dict(err: dict[str, Any], raw: str) -> SocialError:
    try:
        code = int(err.get("code", 0) or 0)
    except (TypeError, ValueError):
        code = 0
    message = _ig_message(raw)
    if code in (102, 190):
        return AuthError(f"instagram graph {code}: {message}")
    if code in (10, 200):
        return PermissionError(f"instagram graph {code}: {message}")
    if code in (4, 17, 32, 341, 613):
        return TransientError(f"instagram graph {code}: {message}")
    if code:
        return SocialError(f"instagram graph {code}: {message}",
                           code="provider_error",
                           user_message="Instagram mengembalikan error.")
    return SocialError(f"instagram error: {message}", code="provider_error",
                       user_message="Instagram mengembalikan error.")


def _classify_ig_http_error(http_status: int, detail: str) -> SocialError:
    try:
        data = json.loads(detail or "{}")
        err = data.get("error", {}) if isinstance(data, dict) else {}
    except ValueError:
        err = {}
    if isinstance(err, dict) and err.get("code"):
        return _classify_ig_error_dict(err, detail)
    return classify_http_status(http_status, _ig_message(detail))


# -- publisher --------------------------------------------------------------

class InstagramLoginPublisher(SocialPublisher):
    """Instagram Reels publishing via Instagram Login. No Facebook involved."""

    provider = "instagram"
    capabilities = INSTAGRAM_LOGIN_CAPABILITIES

    # -- accounts ------------------------------------------------------
    # Flow: Instagram Login (user token) -> GET graph.instagram.com/me.
    # The IG-scoped user id IS the publishing account id. Stored
    # explicitly: ig_user_id + username + account_type. Never a Page id,
    # never a page_token, never /me/accounts.
    def account_info(self, access_token: str) -> AccountInfo:
        data = _IGraphClient.call(
            "GET", "me", access_token,
            {"fields": "id,username,account_type"})
        ig_id = str(data.get("id", "") or "")
        username = str(data.get("username", "") or "")
        account_type = str(data.get("account_type", "") or "").upper()
        if not ig_id:
            raise AuthError("instagram account lookup returned no id")
        self._require_professional(
            account_type, has_username=bool(username))
        return AccountInfo(
            external_account_id=ig_id,
            display_name="@" + username if username else ig_id,
            extra={"ig_user_id": ig_id, "username": username,
                   "account_type": account_type,
                   "login_flow": LOGIN_FLOW_MARKER})

    def validate_account(self, access_token: str, account_meta: dict) -> None:
        info = self.account_info(access_token)
        account_meta["display_name"] = info.display_name

    @staticmethod
    def _require_professional(account_type: str,
                              has_username: bool = True) -> None:
        # account_type is a safe enum string; including the actual value in
        # the server-log detail tells PERSONAL apart from a missing field.
        if account_type not in PROFESSIONAL_ACCOUNT_TYPES:
            raise AccountRequirementError(
                "Instagram account does not meet publishing requirements "
                f"(account_type={account_type or 'empty'} "
                f"has_username={has_username}).")

    # -- publish (video_url container OR resumable -> poll -> publish) --
    def publish(self, ctx: PublishContext) -> PublishResult:
        from ..states import PUBLISHED

        if is_legacy_instagram_account(ctx.account_meta):
            raise SocialError(
                "instagram connection uses the retired Facebook Login flow",
                code="legacy_instagram_connection", retryable=False,
                user_message="Koneksi Instagram lama tidak berlaku. Klik Reconnect lalu login via Instagram.")
        validate_clip_file(ctx.clip_path, max_bytes=MAX_BYTES)
        token = ctx.access_token()
        ig_id = (ctx.account_meta.get("ig_user_id")
                 or ctx.account_meta.get("external_account_id")
                 or self.account_info(token).external_account_id)
        total = ctx.clip_path.stat().st_size
        caption = ((ctx.request.caption or "") +
                   ("\n" + " ".join(ctx.request.hashtags)
                    if ctx.request.hashtags else "")).strip()[:2200]
        media_url = str(ctx.account_meta.get("media_url", "") or "")
        if media_url:
            # Documented Instagram Login path: Meta fetches the file from
            # a public https URL (served job-scoped through our tunnel).
            # The URL value itself is never logged.
            logger.info("[SOCIAL_PUBLISH] provider=instagram "
                        "clip_id=%s via=video_url", ctx.request.clip_id)
            container_id = self._create_url_container(
                token, ig_id, media_url, caption)
        else:
            container_id, upload_uri = self._create_resumable_container(
                token, ig_id, caption)
            self._transfer_bytes(ctx, token, container_id, upload_uri, total)
        try:
            ctx.on_progress(1.0, "uploading")
            ctx.on_progress(1.0, "finalizing")
        except Exception:
            pass
        final_id = self._wait_and_publish(ctx, token, ig_id, container_id)
        try:
            ctx.on_uploaded(final_id, "")
        except Exception:
            pass
        logger.info("[SOCIAL_PUBLISH] provider=instagram clip_id=%s status=published",
                    ctx.request.clip_id)
        ctx.on_progress(1.0, "published")
        return PublishResult(status=PUBLISHED, remote_media_id=final_id)

    def get_publish_status(self, access_token: str, remote_media_id: str) -> str:
        from ..states import FAILED, PROCESSING, PUBLISHED

        try:
            data = _IGraphClient.call(
                "GET", remote_media_id, access_token,
                {"fields": "id,status_code"})
        except SocialError as exc:
            # A published MEDIA object has no status_code field (container
            # field). Fall back to a media-shaped read: object exists and
            # is readable as media => PUBLISHED. Anything else re-raises.
            if getattr(exc, "code", "") != "provider_error" \
                    or "status_code" not in str(exc):
                raise
            media = _IGraphClient.call(
                "GET", remote_media_id, access_token,
                {"fields": "id,media_type,timestamp"})
            if str(media.get("id", "") or ""):
                return PUBLISHED
            raise
        code = str(data.get("status_code", "FINISHED")).upper()
        if code in ("ERROR", "EXPIRED"):
            return FAILED
        if code in ("IN_PROGRESS",):
            return PROCESSING
        return PUBLISHED

    # -- Instagram Login API calls (graph.instagram.com only) ---------
    def _create_url_container(self, token: str, ig_id: str,
                              video_url: str, caption: str) -> str:
        """Create a standard video container Meta fetches from video_url.

        Documented Instagram Login shape: media_type=REELS + publicly
        accessible https video_url (+ caption). Returns the container id.
        """
        params: dict[str, Any] = {"media_type": "REELS",
                                  "video_url": video_url,
                                  "share_to_feed": True}
        if caption:
            params["caption"] = caption
        data = _IGraphClient.call("POST", f"{ig_id}/media", token, params)
        container = str(data.get("id", "") or "")
        if not container:
            raise TransientError("instagram container creation malformed")
        # Container/media IDs are plain object IDs (safe to log, like the
        # remote id). Keys show what creation actually returned.
        logger.info("[SOCIAL_PUBLISH] provider=instagram stage=container "
                    "container_id=%s response_keys=%s",
                    container, sorted(str(k) for k in data.keys()))
        return container

    def _create_resumable_container(self, token: str, ig_id: str,
                                    caption: str) -> tuple[str, str]:
        """Create a resumable container. Returns (container_id, upload_uri).

        The upload session URL is RETURNED by Meta (uri field), never
        constructed — only the documented fallback construction is used
        when uri is absent. Documented params only: media_type, upload_type,
        caption, share_to_feed (+access_token auth).
        """
        params: dict[str, Any] = {"media_type": "REELS",
                                  "upload_type": "resumable",
                                  "share_to_feed": True}
        if caption:
            params["caption"] = caption
        try:
            data = _IGraphClient.call("POST", f"{ig_id}/media", token, params)
        except SocialError as exc:
            # Diagnostic host probe (ONE attempt, not a retry loop): the
            # resumable-container flow is documented against
            # graph.facebook.com; graph.instagram.com is documented for
            # Instagram Login. If the primary host rejects resumable with
            # "video_url required", try the documented resumable host once
            # with identical params. Any other error fails fast.
            if getattr(exc, "code", "") != "provider_error" \
                    or "video_url" not in str(exc):
                raise
            logger.info("[SOCIAL_PUBLISH] provider=instagram "
                        "container_create trying graph.facebook.com once")
            try:
                data = _IGraphClient.call(
                    "POST", f"{ig_id}/media", token, params, host=FB_GRAPH)
            except SocialError as exc2:
                # Either way the resumable path is unusable for this
                # login: same video_url rejection, or the IG token being
                # unreadable on the facebook host (auth 190). Surface the
                # honest terminal code — a reconnect would not help.
                if (getattr(exc2, "code", "") == "provider_error"
                        and "video_url" in str(exc2)) \
                        or isinstance(exc2, AuthError):
                    raise SocialError(
                        "instagram resumable upload not accepted for this "
                        f"login (fb host: {getattr(exc2, 'code', '?')})",
                        code="instagram_resumable_unsupported",
                        retryable=False,
                        user_message="Instagram Login membutuhkan URL video publik untuk path ini.") from exc2
                raise
        container = str(data.get("id", "") or "")
        uri = str(data.get("uri", "") or "")
        if not container:
            raise TransientError("instagram container creation malformed")
        if not uri:
            uri = (f"{IG_RUPLOAD}/ig-api-upload/"
                   f"{IG_GRAPH_VERSION}/{container}")
        return container, uri

    def _transfer_bytes(self, ctx: PublishContext, token: str,
                        container_id: str, upload_uri: str,
                        total: int) -> None:
        """Upload local bytes to the container's upload session URL.

        Exact documented shape (IG User Media reference, resumable flow):
          POST {uri}  (uri returned by container creation)
          Authorization: OAuth <IG User token>
          offset: 0
          file_size: <total bytes>
          Content-Type: application/octet-stream, whole file, one POST
        Auth/permission rejections fail fast as honest provider errors
        (never retried as transient); transport loss is transient so the
        bounded service retry can re-attempt. No session URL, token, or
        header values are logged — only safe stage metadata.
        """
        ctx.check_cancelled()
        with open(ctx.clip_path, "rb") as handle:
            body = handle.read()
        if len(body) != total:
            raise TransientError("instagram clip changed during upload")
        # The uri is trusted only when it targets the documented upload
        # host; otherwise fall back to the documented construction.
        try:
            host = urllib.parse.urlparse(upload_uri).netloc.lower()
        except Exception:
            host = ""
        url = upload_uri if host == "rupload.facebook.com" else (
            f"{IG_RUPLOAD}/ig-api-upload/{IG_GRAPH_VERSION}/{container_id}")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/octet-stream",
                     "Content-Length": str(len(body)),
                     "Authorization": f"OAuth {token}",
                     "offset": "0",
                     "file_size": str(total)})
        logger.info("[SOCIAL_PUBLISH] provider=instagram stage=upload "
                    "progress=0%% bytes=%d", total)
        try:
            with urllib.request.urlopen(request, timeout=300):
                pass
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode(
                    "utf-8", errors="replace")[:500]
            except Exception:
                detail = ""
            err = _classify_ig_http_error(
                exc.code, detail or "instagram transfer failed")
            logger.info("[SOCIAL_PUBLISH] provider=instagram stage=upload "
                        "http_status=%d code=%s", exc.code,
                        getattr(err, "code", "?"))
            raise err from exc
        except OSError as exc:
            raise TransientError(
                f"instagram transfer unreachable: {exc}") from exc
        ctx.on_progress(1.0, "uploading")
        logger.info("[SOCIAL_PUBLISH] provider=instagram stage=upload "
                    "progress=100%%")

    def _wait_and_publish(self, ctx: PublishContext, token: str,
                          ig_id: str, container_id: str) -> str:
        """Poll container to FINISHED, then media_publish. Bounded waits.

        Poll reads are tolerant: a freshly created container may briefly
        answer errors (e.g. code 100 "nonexisting field (status_code)"
        before Meta materializes it) — those count as IN_PROGRESS until
        attempts run out. Only an explicit ERROR/EXPIRED status fails
        fast; exhaustion raises a retryable timeout (service retry then
        starts a clean new container, never a duplicate publish).
        """
        from ..states import FAILED

        for attempt in range(12):
            ctx.check_cancelled()
            try:
                data = _IGraphClient.call(
                    "GET", container_id, token,
                    {"fields": "id,status_code,status"})
            except SocialError as exc:
                # Fresh containers may briefly 400 (materialization race).
                # Tolerate the first few polls (~40s); a persistently
                # unreadable container fails fast with the real error.
                logger.info("[SOCIAL_PUBLISH] provider=instagram "
                            "container poll container_id=%s attempt=%d code=%s (treating as processing)",
                            container_id, attempt + 1,
                            getattr(exc, "code", "?"))
                if attempt >= 4:
                    raise
                data = {}
            code = str(data.get("status_code", "IN_PROGRESS")).upper()
            if attempt == 0:
                logger.info("[SOCIAL_PUBLISH] provider=instagram "
                            "container poll first container_id=%s keys=%s code=%s",
                            container_id,
                            sorted(str(k) for k in data.keys()), code)
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                subcode = str(data.get("status", "") or "")
                raise SocialError(
                    f"instagram container {code.lower()} subcode={subcode}",
                    code="processing_failed",
                    user_message="Instagram gagal memproses video ini.")
            if attempt < 11:
                time.sleep(10)
        else:
            raise TransientError("instagram container processing timeout")
        done = _IGraphClient.call("POST", f"{ig_id}/media_publish", token,
                                  {"creation_id": container_id})
        published = str(done.get("id", "") or "")
        if not published:
            raise TransientError("instagram publish malformed")
        status = self.get_publish_status(token, published)
        if status == FAILED:
            raise SocialError("instagram publish failed",
                              code="processing_failed",
                              user_message="Instagram gagal memproses video ini.")
        return published
