"""Desktop OAuth infrastructure (spec §5-§7).

Loopback-only flow, no public server, no tunnels:

1. Backend builds the provider authorization URL (state + PKCE) and hands
   it to the UI, which opens the SYSTEM browser.
2. Backend listens on 127.0.0.1:<ephemeral> for ONE callback request,
   validates state, captures the authorization code, then closes the
   listener (never permanent, never 0.0.0.0).
3. Backend exchanges the code (PKCE verifier where supported) and stores
   credentials via SecretStore. Codes/tokens are never logged.

Providers that officially require a server-side component are NOT faked —
their connect flow raises ApprovalRequiredError with an explicit note.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import secrets
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from .states import ApprovalRequiredError, AuthError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Official endpoints + scopes for one provider's desktop OAuth."""

    provider: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()
    # Extra authorize params (access_type=offline, prompt=consent, ...).
    extra_authorize_params: dict[str, str] = field(default_factory=dict)
    use_pkce: bool = True
    # Google installed-app style: no client secret required.
    client_secret_required: bool = False
    # Form field name carrying the client identity. Google/Meta use
    # "client_id"; TikTok Login Kit uses "client_key".
    client_id_param: str = "client_id"
    # Forced redirect hostname for this provider, or None to keep the
    # caller's host. Meta ONLY exempts the literal hostname "localhost"
    # from its HTTPS enforcement (dev-mode apps); the IP literal
    # 127.0.0.1 is treated as an insecure non-localhost URL and rejected
    # with "isn't using a secure connection". Google/TikTok accept both,
    # so they keep None (caller's host preserved, zero behavior change).
    redirect_host: str | None = None
    # Set when the platform officially needs server-side OAuth: connect
    # is refused with this note instead of an unofficial workaround.
    server_only_note: str = ""
    # Separator for the scope authorize param. OAuth default is a space;
    # Instagram Login (www.instagram.com/oauth/authorize) documents
    # comma-separated scopes, so it sets ",".
    scope_separator: str = " "


def apply_redirect_host(redirect_uri: str,
                        config: OAuthProviderConfig) -> str:
    """Rewrite a loopback redirect_uri to the provider's required hostname.

    Only applies when config.redirect_host is set AND the incoming host is
    itself loopback (127.0.0.1/localhost) — never rewrites anything else,
    so this cannot become an open redirector. Port and path preserved,
    which keeps the token-exchange redirect_uri identical.
    """
    if not config.redirect_host:
        return redirect_uri
    try:
        import urllib.parse

        parsed = urllib.parse.urlparse(redirect_uri or "")
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            return redirect_uri
        netloc = config.redirect_host
        if parsed.port:
            netloc += f":{parsed.port}"
        return urllib.parse.urlunparse(
            (parsed.scheme, netloc, parsed.path, "", parsed.query, ""))
    except Exception:
        return redirect_uri


@dataclass
class OAuthSession:
    provider: str
    state: str
    code_verifier: str = ""
    code_challenge: str = ""
    redirect_uri: str = ""
    listen_port: int = 0
    created_at: float = 0.0
    authorization_url: str = ""
    # Filled by the callback handler.
    code: str = ""
    callback_error: str = ""


def new_pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorization_url(client_id: str, config: OAuthProviderConfig,
                            redirect_uri: str) -> OAuthSession:
    """Create state (+PKCE) and the URL the UI opens in the system browser."""
    if config.server_only_note:
        raise ApprovalRequiredError(config.server_only_note)
    session = OAuthSession(
        provider=config.provider,
        state=secrets.token_urlsafe(24),
        redirect_uri=redirect_uri,
        created_at=time.time(),
    )
    params: dict[str, str] = {
        config.client_id_param: client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": session.state,
    }
    if config.scopes:
        params["scope"] = (config.scope_separator or " ").join(config.scopes)
    if config.use_pkce:
        verifier, challenge = new_pkce_pair()
        session.code_verifier = verifier
        session.code_challenge = challenge
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    params.update(config.extra_authorize_params)
    query = urllib.parse.urlencode(params)
    session.authorization_url = f"{config.authorize_url}?{query}"
    return session


class _CallbackHandler(BaseHTTPRequestHandler):
    sessions: dict[str, OAuthSession] = {}
    server_version = "GaeclipOAuth"

    def log_message(self, *args: Any) -> None:  # keep stdlib http quiet
        return

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        state = (query.get("state") or [""])[0]
        session = self.sessions.get(state)
        if session is None:
            self._respond(400, "Unknown or expired login session. You can close this tab.")
            return
        error = (query.get("error") or [""])[0]
        if error:
            session.callback_error = error
            self._respond(400, f"Login declined ({html.escape(error)}). You can close this tab.")
            return
        code = (query.get("code") or [""])[0]
        if not code:
            self._respond(400, "Missing authorization code. You can close this tab.")
            return
        session.code = code
        self._respond(200, "Login successful. You can close this tab and return to GAEClip.")

    def _respond(self, status: int, text: str) -> None:
        body = f"<html><body><p>{text}</p></body></html>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


def bind_loopback(host: str = "127.0.0.1", port: int = 0) -> HTTPServer:
    """Bind the OAuth callback listener (caller closes).

    Defaults are loopback-only + ephemeral port (desktop-safe). Docker on
    the same machine as the browser may configure a fixed port published
    as 127.0.0.1:PORT:PORT; the host side stays loopback-only.
    """
    return HTTPServer((host, port), _CallbackHandler)


def await_callback(server: HTTPServer, session: OAuthSession,
                   timeout_seconds: float = 600) -> str:
    """Serve until the registered state arrives, declines, or times out.
    Always shuts the listener down."""
    _CallbackHandler.sessions[session.state] = session
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.1},
                              name="gaeclip-oauth-callback", daemon=True)
    thread.start()
    deadline = time.time() + max(5.0, timeout_seconds)
    try:
        while time.time() < deadline:
            if session.code or session.callback_error:
                break
            time.sleep(0.1)
    finally:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        _CallbackHandler.sessions.pop(session.state, None)
        thread.join(timeout=5)
    if session.callback_error:
        raise AuthError(f"oauth declined: {session.callback_error}",
                        user_message="Login dibatalkan atau ditolak.")
    if not session.code:
        raise AuthError("oauth callback timed out",
                        user_message="Login kedaluwarsa. Coba hubungkan lagi.")
    return session.code


def wait_for_callback(session: OAuthSession, timeout_seconds: float = 600) -> str:
    """Serve ONE callback on 127.0.0.1:<ephemeral>. Returns the code.

    Raises AuthError on timeout/decline/state mismatch. The listener is
    always shut down (reviews: no permanent port, no 0.0.0.0).
    """
    server = bind_loopback()
    session.listen_port = server.server_address[1]
    session.redirect_uri = f"http://127.0.0.1:{session.listen_port}/oauth/callback"
    return await_callback(server, session, timeout_seconds)


def exchange_code(*, token_url: str, code: str, redirect_uri: str,
                  client_id: str, client_secret: str = "",
                  code_verifier: str = "",
                  client_id_param: str = "client_id") -> dict[str, Any]:
    """Authorization-code -> tokens. Secrets stay in POST body, never logs."""
    payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        client_id_param: client_id,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    if code_verifier:
        payload["code_verifier"] = code_verifier
    return _post_form(token_url, payload)


def refresh_access_token(*, token_url: str, refresh_token: str,
                         client_id: str, client_secret: str = "",
                         client_id_param: str = "client_id") -> dict[str, Any]:
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        client_id_param: client_id,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    return _post_form(token_url, payload)


def _post_form(url: str, fields: dict[str, str]) -> dict[str, Any]:
    """application/x-www-form-urlencoded POST -> parsed JSON."""
    import urllib.error

    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        # Never include request fields (may hold secrets) in the error.
        raise AuthError(f"token endpoint http {exc.code}: {detail}",
                        user_message=_token_error_message(detail)) from exc
    except OSError as exc:
        from .states import TransientError

        raise TransientError(f"token endpoint unreachable: {exc}") from exc
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise AuthError("token endpoint returned invalid JSON") from exc
    if not isinstance(parsed, dict) or "access_token" not in parsed:
        raise AuthError("token endpoint response missing access_token")
    return parsed


def _token_error_message(detail: str) -> str:
    """Map provider token errors to actionable Indonesian messages.

    Only the standard `error`/`error_description` fields are surfaced —
    never raw bodies or request material.
    """
    try:
        data = json.loads(detail or "{}")
    except ValueError:
        return "Gagal menukar kode login. Coba lagi."
    err = str(data.get("error", "")).lower()
    desc = str(data.get("error_description", ""))[:200]
    # Meta Invalid Scopes (authorize-step misconfiguration surfacing at
    # token time on some flows): guide to Settings instead of raw dump.
    if "invalid" in err and "scope" in (err + " " + desc.lower()):
        combined = (err + " " + desc.lower())
        if "publish_video" in combined:
            return ("Meta menolak permission publish_video (khusus Live Video API, "
                    "tidak dibutuhkan untuk upload Reels). Update ke versi Gaeclip "
                    "terbaru yang sudah menghapusnya, lalu klik Connect lagi.")
        return ("Meta menolak permission yang diminta (Invalid Scopes). Periksa: "
                "1) App Type harus Business/None bukan Consumer, "
                "2) tambah Use Case 'Manage everything on your Page' (+ Instagram bila perlu), "
                "3) product Facebook Login ON di App Dashboard.")
    if "redirect_uri_mismatch" in err or "redirect_uri_mismatch" in desc:
        return ("Redirect tidak cocok: tipe OAuth client harus Desktop app "
                "dan redirect loopback diizinkan.")
    if err == "invalid_grant":
        return ("Kode login kedaluwarsa/terpakai. Klik Connect lagi dan "
                "selesaikan dalam sekali jalan.")
    if err in ("access_denied",):
        return "Login dibatalkan atau ditolak."
    if err == "invalid_client":
        return "Client ID/Secret salah. Periksa Kredensial OAuth di Settings."
    if err == "unauthorized_client":
        return "Client tidak diizinkan untuk flow ini. Periksa tipe aplikasi."
    if desc:
        return f"Gagal menukar kode login: {desc}"
    if err:
        return f"Gagal menukar kode login ({err}). Coba lagi."
    return "Gagal menukar kode login. Coba lagi."


def normalize_credentials(token_response: dict[str, Any],
                          scopes: tuple[str, ...] = ()) -> dict[str, Any]:
    """token response -> stored credential envelope (no secrets logged)."""
    credentials: dict[str, Any] = {
        "access_token": str(token_response.get("access_token", "")),
        "token_type": str(token_response.get("token_type", "Bearer")),
        "scopes": list(scopes),
        "obtained_at": time.time(),
    }
    refresh = token_response.get("refresh_token")
    if refresh:
        credentials["refresh_token"] = str(refresh)
    try:
        credentials["expires_at"] = time.time() + float(token_response.get("expires_in", 3600))
    except (TypeError, ValueError):
        credentials["expires_at"] = time.time() + 3600
    extra = token_response.get("id_token")
    if isinstance(extra, str) and extra:
        credentials["id_token"] = extra
    return credentials


def authorization_url_of(session: OAuthSession) -> str:
    return session.authorization_url
