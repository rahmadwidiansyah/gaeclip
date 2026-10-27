"""OAuth connect orchestration: session registry + browser handoff.

Two callback modes (redirect_uri decides):

1. FRONTEND callback (UI default): redirect_uri points at the app's own
   /oauth/callback page (e.g. http://127.0.0.1:3000/oauth/callback).
   start() returns the authorization_url SYNCHRONOUSLY — the UI navigates
   the SAME tab (never popup-blocked). Google lands back in-app, the page
   forwards {state, code} to POST /oauth/callback, backend exchanges and
   stores. No listener, no ports, works behind Docker too.
2. LOOPBACK listener (API-only clients): no redirect_uri given — backend
   binds 127.0.0.1:<ephemeral>, waits, exchanges, same finish path.

Sessions are in-memory only (never persisted, never logged with secrets)
and reaped after completion.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from . import oauth as _oauth
from .states import AuthError

logger = logging.getLogger(__name__)

_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()
_SESSION_TTL = 600.0


def _gc() -> None:
    now = time.time()
    dead = [key for key, item in _sessions.items()
            if now - item.get("created_at", now) > _SESSION_TTL
            and item.get("status") != "pending"]
    for key in dead:
        _sessions.pop(key, None)


def _allowed_public_origins() -> set[str]:
    """Operator-trusted https origins (same list as CORS FRONTEND_ORIGINS).

    Lets a tunneled dev frontend (e.g. ngrok HTTPS) receive the OAuth
    callback. Loopback http is always allowed; anything else must be an
    exact scheme://host[:port] entry here — never arbitrary URLs (prevents
    laundering OAuth codes through this backend).
    """
    import os
    import urllib.parse

    allowed: set[str] = set()
    for entry in os.getenv("FRONTEND_ORIGINS", "").split(","):
        entry = entry.strip().rstrip("/")
        if not entry:
            continue
        try:
            parsed = urllib.parse.urlparse(entry)
        except Exception:
            continue
        if parsed.scheme == "https" and parsed.hostname:
            netloc = parsed.hostname.lower()
            if parsed.port:
                netloc += f":{parsed.port}"
            allowed.add(f"https://{netloc}")
    return allowed


def _valid_frontend_redirect(redirect_uri: str) -> bool:
    """Loopback /oauth/callback, or an allowlisted https /oauth/callback.

    Loopback http is always accepted. Public https is accepted only when
    its origin exactly matches a FRONTEND_ORIGINS entry — never an
    arbitrary URL (prevents laundering OAuth codes through this backend).
    """
    try:
        import urllib.parse

        parsed = urllib.parse.urlparse(redirect_uri or "")
        if parsed.path != "/oauth/callback":
            return False
        if (parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "localhost")):
            return True
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        netloc = parsed.hostname.lower()
        if parsed.port:
            netloc += f":{parsed.port}"
        return f"https://{netloc}" in _allowed_public_origins()
    except Exception:
        return False


def start(client: Any, provider: str, client_id: str,
          client_secret: str = "", redirect_uri: str = "") -> dict[str, Any]:
    """Create a pending session. With a valid frontend redirect_uri the
    authorization URL is returned IMMEDIATELY (no listener, no thread) —
    the UI navigates the same tab. Otherwise the loopback-listener flow
    runs in a worker thread as before."""
    from .service import _provider_oauth_config

    config = _provider_oauth_config(provider)
    session_id = f"oauth_{uuid.uuid4().hex[:12]}"
    # Pre-register so the waiter thread and poller share state.
    with _lock:
        _gc()
        _sessions[session_id] = {"status": "pending", "provider": provider,
                                 "created_at": time.time(), "account_id": "",
                                 "error": ""}
    if redirect_uri:
        from .states import SocialError

        if not _valid_frontend_redirect(redirect_uri):
            with _lock:
                _sessions.pop(session_id, None)
            raise SocialError("invalid redirect_uri (loopback /oauth/callback only)",
                              code="invalid_input",
                              user_message="Redirect login tidak valid.")
        # Provider-specific hostname policy (Meta: localhost only).
        redirect_uri = _oauth.apply_redirect_host(redirect_uri, config)
        session = _oauth.build_authorization_url(client_id, config, redirect_uri)
        try:
            # SAFE log: provider + redirect + scopes only (no secrets/tokens).
            tag = "[INSTAGRAM_LOGIN]" if provider == "instagram" else "[META_OAUTH]"
            logger.info("%s provider=%s redirect=%s scopes=%s authorize=%s",
                        tag, provider, redirect_uri, ",".join(config.scopes),
                        getattr(config, "authorize_url", ""))
        except Exception:
            pass
        with _lock:
            item = _sessions.get(session_id)
            if item is not None:
                item["oauth"] = session
                item["client_id"] = client_id
                item["client_secret"] = client_secret
                item["authorization_url"] = session.authorization_url
        return {"session_id": session_id,
                "authorization_url": session.authorization_url}
    thread = threading.Thread(
        target=_run_flow,
        args=(client, provider, client_id, client_secret, session_id),
        name=f"gaeclip-oauth-{session_id}", daemon=True)
    thread.start()
    # The authorization URL needs the loopback redirect URI, which is only
    # known once the listener binds — poll endpoint exposes it immediately.
    return {"session_id": session_id}


def _safe_suffix(value: str, keep: int = 6) -> str:
    """Last-N chars for session/state ids (never log full secrets)."""
    try:
        text = str(value or "")
        return text[-keep:] if len(text) > keep else text
    except Exception:
        return ""


def _infer_stage(exc: Exception) -> str:
    """exchange vs account_info vs unknown — for safe server logs only."""
    try:
        staged = str(getattr(exc, "_oauth_stage", "") or "")
        if staged in ("exchange", "account_info", "authorize", "callback", "session"):
            return staged
        text = str(exc).lower()
    except Exception:
        return "unknown"
    if "token endpoint" in text or "authorization_code" in text:
        return "exchange"
    code = str(getattr(exc, "code", "") or "").lower()
    if code in ("invalid_request", "account_requirement", "permission_denied",
                "provider_error", "transient"):
        return "account_info"
    if "me/accounts" in text or "instagram_business_account" in text:
        return "account_info"
    return "unknown"


def _tag_stage(exc: Exception, stage: str) -> Exception:
    """Attach a log-only stage tag without touching user_message/code."""
    try:
        setattr(exc, "_oauth_stage", stage)
    except Exception:
        pass
    return exc


def complete_by_state(client: Any, state: str, code: str = "",
                      error: str = "") -> dict[str, Any]:
    """Finish a frontend-callback flow.

    Idempotent + race-safe: concurrent duplicate POSTs (StrictMode,
    refresh, double click) never redeem the same code twice — the loser
    waits for the winner and shares its outcome.
    """
    with _lock:
        found = [(sid, item) for sid, item in _sessions.items()
                 if isinstance(item, dict)
                 and getattr(item.get("oauth"), "state", None) == state
                 and item.get("status") in ("pending", "completing", "complete")]
    if not found:
        logger.info("[SOCIAL_AUTH] connect failed stage=session code=unknown_session detail=state~%s",
                    _safe_suffix(state))
        raise AuthError("unknown or expired login session",
                        user_message="Sesi login tidak dikenal/kedaluwarsa. Mulai lagi dari Connect.")
    session_id, item = found[0]
    if item.get("status") == "complete":
        return {"status": "complete",
                "account_id": item.get("account_id", ""), "error": ""}
    if item.get("status") == "completing":
        return _await_winner(session_id)
    with _lock:
        live = _sessions.get(session_id)
        if live is None or live.get("status") != "pending":
            # Lost the race after all: re-read final outcome.
            return complete_by_state(client, state, code, error)
        live["status"] = "completing"
    session = live["oauth"]
    provider = str(live.get("provider", "") or "")
    if error:
        with _lock:
            done = _sessions.get(session_id)
            if done is not None:
                done.update(status="failed",
                            error="Login dibatalkan atau ditolak.")
        logger.info("[SOCIAL_AUTH] provider=%s stage=authorize code=declined detail=%s",
                    provider, str(error)[:80])
        raise AuthError(f"oauth declined: {error}",
                        user_message="Login dibatalkan atau ditolak.")
    if not code:
        _mark_failed(session_id, "Kode login hilang. Coba lagi.")
        logger.info("[SOCIAL_AUTH] provider=%s stage=callback code=missing_code detail=session~%s",
                    provider, _safe_suffix(session_id))
        raise AuthError("missing authorization code",
                        user_message="Kode login hilang. Coba lagi.")
    try:
        _finish(client, live.get("provider", ""), live.get("client_id", ""),
                live.get("client_secret", ""), session, code, session_id)
    except Exception as exc:
        message = getattr(exc, "user_message", None) or "Login gagal. Coba lagi."
        _mark_failed(session_id, message)
        # Server-side diagnostics only: technical detail (HTTP status +
        # provider reason, never codes/tokens/secrets) goes to the log;
        # the UI/store keeps only the generic user-facing message.
        try:
            logger.info("[SOCIAL_AUTH] provider=%s stage=%s code=%s detail=%s",
                        provider, _infer_stage(exc),
                        getattr(exc, "code", "error"),
                        str(exc)[:300])
        except Exception:
            pass
        raise
    with _lock:
        done = _sessions.get(session_id) or {}
    return {"status": done.get("status", "failed"),
            "account_id": done.get("account_id", ""),
            "error": done.get("error", "")}


def _await_winner(session_id: str, timeout_seconds: float = 60) -> dict[str, Any]:
    """Wait for the in-flight completion, then share its outcome."""
    deadline = time.time() + max(5.0, timeout_seconds)
    while time.time() < deadline:
        time.sleep(0.5)
        with _lock:
            live = _sessions.get(session_id) or {}
            status = live.get("status")
            if status == "complete":
                return {"status": "complete",
                        "account_id": live.get("account_id", ""), "error": ""}
            if status == "failed":
                raise AuthError(live.get("error") or "Login gagal.",
                                user_message=live.get("error") or "Login gagal.")
    raise AuthError("login completion timed out",
                    user_message="Login belum selesai. Coba lagi.")


def _mark_failed(session_id: str, message: str) -> None:
    with _lock:
        item = _sessions.get(session_id)
        if item is not None:
            item.update(status="failed", error=message)


def poll(service: Any, session_id: str) -> dict[str, Any]:
    """Session status for the UI poller (no secrets in output)."""
    with _lock:
        item = _sessions.get(session_id)
        snapshot = dict(item) if item else None
    if snapshot is None:
        return {"status": "failed", "error": "Sesi login tidak ditemukan."}
    out: dict[str, Any] = {"status": snapshot["status"],
                            "provider": snapshot.get("provider", "")}
    if snapshot["status"] == "pending":
        out["authorization_url"] = snapshot.get("authorization_url", "")
    elif snapshot["status"] == "complete":
        out["account_id"] = snapshot.get("account_id", "")
    else:
        out["error"] = snapshot.get("error", "Login gagal.")
    return out


def _oauth_bind(client: Any) -> tuple[str, int]:
    """(host, port) for the callback listener from Settings.

    Defaults 127.0.0.1:0 (loopback, ephemeral). Docker on the same
    machine as the browser: 0.0.0.0:<fixed> + publish 127.0.0.1:PORT:PORT.
    """
    try:
        from ..app_settings import AppSettings

        settings = AppSettings(client)
        host = str(settings.get("SOCIAL_OAUTH_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        try:
            port = int(settings.get("SOCIAL_OAUTH_PORT") or 0)
        except (TypeError, ValueError):
            port = 0
        if not 0 <= port <= 65535:
            port = 0
        return host, port
    except Exception:
        return "127.0.0.1", 0


def _run_flow(client: Any, provider: str, client_id: str,
              client_secret: str, session_id: str) -> None:
    from .service import _provider_oauth_config

    config = _provider_oauth_config(provider)
    server = None
    try:
        # Bind loopback FIRST so redirect_uri is real before the user
        # opens the browser. Listener lives only for this flow.
        # Resolver consistency: bind the SAME hostname the redirect uses
        # (Meta requires literal "localhost"; binding the IP while the
        # browser resolves ::1 would refuse the connection).
        host, port = _oauth_bind(client)
        if config.redirect_host and host in ("127.0.0.1", "localhost", ""):
            host = config.redirect_host
        server = _oauth.bind_loopback(host, port)
        port = server.server_address[1]
        redirect_uri = _oauth.apply_redirect_host(
            f"http://{host}:{port}/oauth/callback", config)
        session = _oauth.build_authorization_url(client_id, config, redirect_uri)
        session.listen_port = port
        with _lock:
            item = _sessions.get(session_id)
            if item is not None:
                item["authorization_url"] = session.authorization_url
        code = _oauth.await_callback(server, session, timeout_seconds=600)
        server = None  # await_callback already closed it
        _finish(client, provider, client_id, client_secret, session,
                code, session_id)
    except Exception as exc:
        message = getattr(exc, "user_message", None) or "Login gagal. Coba lagi."
        try:
            logger.info("[SOCIAL_AUTH] provider=%s stage=%s code=%s detail=%s", provider,
                        _infer_stage(exc), getattr(exc, "code", "error"),
                        str(exc)[:300])
        except Exception:
            pass
        with _lock:
            item = _sessions.get(session_id)
            if item is not None:
                item.update(status="failed", error=message)
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass


def _finish(client: Any, provider: str, client_id: str, client_secret: str,
            session: Any, code: str, session_id: str) -> None:
    # Instagram Login has its own token shapes (wrapped code response,
    # long-lived upgrade + IG-scoped user id) — dedicated path below.
    # It never touches Facebook endpoints, Pages, or page tokens.
    if provider == "instagram":
        _finish_instagram_login(client, client_id, client_secret,
                                session, code, session_id)
        return
    from .accounts import SocialAccount, SocialAccountStore
    from .service import _provider_oauth_config, publisher_for
    from .secrets import make_secret_store

    config = _provider_oauth_config(provider)
    try:
        token_response = _oauth.exchange_code(
            token_url=config.token_url, code=code,
            redirect_uri=session.redirect_uri, client_id=client_id,
            client_secret=client_secret, code_verifier=session.code_verifier,
            client_id_param=config.client_id_param)
    except Exception as exc:
        raise _tag_stage(exc, "exchange") from exc
    if provider == "facebook":
        # Short-lived user tokens die in ~2h (then every publish fails
        # with auth_failed until reconnect). Upgrade to a 60-day token
        # NOW so discovery + stored credentials are both long-lived.
        token_response = _exchange_fb_long_lived(
            config.token_url, token_response, client_id, client_secret)
    credentials = _oauth.normalize_credentials(
        token_response, tuple(config.scopes))
    runtime = _runtime_of(client)
    secrets = make_secret_store(client, runtime)
    publisher = publisher_for(provider)
    try:
        info = publisher.account_info(str(credentials["access_token"]))
        publisher.validate_account(str(credentials["access_token"]), {})
    except Exception as exc:
        raise _tag_stage(exc, "account_info") from exc
    store = SocialAccountStore(client)
    # Reconnect same external account instead of duplicating it.
    account = next((item for item in store.list(provider)
                    if item.external_account_id and
                    item.external_account_id == info.external_account_id), None)
    import time as _time

    if account is None:
        account = SocialAccount(
            id=SocialAccountStore.new_id(provider), provider=provider,
            created_at=_time.time())
    account.external_account_id = info.external_account_id
    account.display_name = info.display_name or account.display_name
    account.status = "connected"
    account.scopes = tuple(config.scopes)
    account.expires_at = credentials.get("expires_at")
    account.meta = {**(account.meta or {}), **(info.extra or {})}
    # Cache public App ID for app-scoped calls (e.g. Resumable Upload
    # POST /{APP_ID}/uploads). Never the secret.
    if client_id and provider in ("facebook", "meta"):
        account.meta["meta_app_id"] = str(client_id)
    account.token_reference = account.token_reference or \
        secrets.new_reference(provider)
    secrets.put(account.token_reference, credentials)
    store.save(account)
    logger.info("[SOCIAL_AUTH] provider=%s account=%s connected",
                provider, account.id)
    with _lock:
        item = _sessions.get(session_id)
        if item is not None:
            item.update(status="complete", account_id=account.id)


def _finish_instagram_login(client: Any, client_id: str,
                              client_secret: str, session: Any,
                              code: str, session_id: str) -> None:
    """Dedicated Instagram Login callback (Business Login for Instagram).

    1. state already validated by complete_by_state / listener.
    2. code -> short-lived IG token (api.instagram.com, wrapped shape).
    3. short -> long-lived (60d, graph.instagram.com).
    4. account discovery via GET graph.instagram.com/me (NO /me/accounts,
       NO Pages, NO page token, NO Facebook user token).
    5. secure storage + metadata. Old Facebook-Login rows are matched by
       IG id and UPGRADED in place (tokens replaced, legacy flags cleared);
       otherwise a fresh account row is created.
    """
    from . import oauth as _oauth
    from .accounts import SocialAccount, SocialAccountStore
    from .publishers.instagram_login import (
        LOGIN_FLOW_MARKER, exchange_code_for_short_lived,
        exchange_for_long_lived)
    from .publishers.instagram_login import (
        InstagramLoginPublisher as _IGPublisher)
    from .secrets import make_secret_store
    from .service import _provider_oauth_config

    config = _provider_oauth_config("instagram")
    try:
        short = exchange_code_for_short_lived(
            client_id=client_id, client_secret=client_secret,
            redirect_uri=session.redirect_uri, code=code)
    except Exception as exc:
        raise _tag_stage(exc, "exchange") from exc
    try:
        long_lived = exchange_for_long_lived(
            str(short["access_token"]), client_secret)
    except Exception as exc:
        raise _tag_stage(exc, "exchange_long_lived") from exc
    credentials = _oauth.normalize_credentials(
        {"access_token": long_lived["access_token"],
         "expires_in": long_lived["expires_in"]},
        tuple(config.scopes))
    credentials["ig_user_id"] = str(short.get("ig_user_id", ""))
    runtime = _runtime_of(client)
    secrets = make_secret_store(client, runtime)
    publisher = _IGPublisher()
    try:
        info = publisher.account_info(str(credentials["access_token"]))
        publisher.validate_account(str(credentials["access_token"]), {})
    except Exception as exc:
        raise _tag_stage(exc, "account_info") from exc
    store = SocialAccountStore(client)
    account = next((item for item in store.list("instagram")
                    if item.external_account_id and
                    item.external_account_id == info.external_account_id), None)
    import time as _time

    if account is None:
        account = SocialAccount(
            id=SocialAccountStore.new_id("instagram"), provider="instagram",
            created_at=_time.time())
    account.external_account_id = info.external_account_id
    account.display_name = info.display_name or account.display_name
    account.status = "connected"
    account.scopes = tuple(config.scopes)
    account.expires_at = credentials.get("expires_at")
    meta = dict(account.meta or {})
    # Upgrade path: drop retired Facebook-Login material, stamp the new
    # flow. Credentials are never deleted silently — the secret itself is
    # replaced only because the same IG account just re-authorized.
    meta.pop("page_token", None)
    meta.pop("facebook_page_id", None)
    meta.pop("page_choices", None)
    meta.pop("legacy_facebook_login", None)
    meta.update(info.extra or {})
    meta["login_flow"] = LOGIN_FLOW_MARKER
    meta["instagram_app_id"] = str(client_id or "")
    account.meta = meta
    account.token_reference = account.token_reference or \
        secrets.new_reference("instagram")
    secrets.put(account.token_reference, credentials)
    store.save(account)
    logger.info("[SOCIAL_AUTH] provider=instagram account=%s connected",
                account.id)
    with _lock:
        item = _sessions.get(session_id)
        if item is not None:
            item.update(status="complete", account_id=account.id)


def _exchange_fb_long_lived(token_url: str, short_response: dict,
                              client_id: str, client_secret: str) -> dict:
    """Swap a short-lived Facebook user token for a 60-day one.

    GET {token_url}?grant_type=fb_exchange_token&... (documented Facebook
    Login flow). Auth failures fail the connect honestly; pure network
    loss falls back to the short token (today's behavior) with a log line
    instead of blocking the login.
    """
    import json as _json
    import urllib.error as _error
    import urllib.parse as _parse
    import urllib.request as _request

    from .states import AuthError

    short_token = str((short_response or {}).get("access_token", "") or "")
    if not short_token:
        return short_response
    query = _parse.urlencode({
        "grant_type": "fb_exchange_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "fb_exchange_token": short_token,
    })
    url = f"{token_url}?{query}"
    request = _request.Request(url, method="GET")
    try:
        with _request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except _error.HTTPError as exc:
        raise AuthError(
            f"facebook long-lived exchange http {exc.code}",
            user_message="Gagal menukar kode login. Coba lagi.") from exc
    except OSError as exc:
        logger.info("[SOCIAL_AUTH] provider=facebook long-lived exchange "
                    "unreachable, keeping short-lived token")
        return short_response
    try:
        parsed = _json.loads(body or "{}")
    except ValueError:
        logger.info("[SOCIAL_AUTH] provider=facebook long-lived exchange "
                    "invalid JSON, keeping short-lived token")
        return short_response
    if not isinstance(parsed, dict) or not parsed.get("access_token"):
        logger.info("[SOCIAL_AUTH] provider=facebook long-lived exchange "
                    "returned no token, keeping short-lived token")
        return short_response
    logger.info("[SOCIAL_AUTH] provider=facebook long-lived token obtained")
    return parsed


def _runtime_of(client: Any) -> str:
    try:
        from ..runtime.factory import is_desktop

        return "desktop" if is_desktop() else "server"
    except Exception:
        return "server"
