"""Optional bearer-token guard for public API deployments.

Local/desktop profiles leave ``API_AUTH_TOKEN`` unset, so this middleware is
a no-op. When the token is configured (required by ``PUBLIC_MODE=true``),
every request except the explicitly public read routes must present either:

    Authorization: Bearer <token>
    X-API-Key: <token>

CORS preflight requests stay unauthenticated so browsers can negotiate.

Public read routes exist because HTML media elements (`<video>`, `<img>`)
and third-party publishers (Instagram/TikTok fetch the clip URL) cannot
attach an Authorization header. They are unguessable-id endpoints:

- ``GET /api/v1/brand-assets/<id>``          (avatar preview)
- ``GET /api/v1/jobs/<id>/clips/<id>``       (clip playback/download)
- ``GET /api/v1/social/publish/<id>/media``  (provider upload source)
"""

from __future__ import annotations

import hmac

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

OPEN_PATHS = frozenset({"/health/live"})


def _is_public_media(method: str, path: str) -> bool:
    """True for header-less media reads (see module docstring)."""
    if method not in {"GET", "HEAD"}:
        return False
    if path.startswith("/api/v1/brand-assets/"):
        return len(path) > len("/api/v1/brand-assets/")
    if path.startswith("/api/v1/jobs/"):
        return "/clips/" in path
    if path.startswith("/api/v1/social/publish/"):
        return path.endswith("/media")
    return False


class ApiTokenMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = (token or "").strip()

    async def dispatch(self, request, call_next):
        if (
            not self._token
            or request.method == "OPTIONS"
            or request.url.path in OPEN_PATHS
            or _is_public_media(request.method, request.url.path)
        ):
            return await call_next(request)

        supplied = self._supplied_token(request)
        if supplied and hmac.compare_digest(
            supplied.encode("utf-8"), self._token.encode("utf-8")
        ):
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={
                "detail": {
                    "code": "UNAUTHORIZED",
                    "message": "API token required (Authorization: Bearer ...)",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    @staticmethod
    def _supplied_token(request) -> str:
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return request.headers.get("x-api-key", "").strip()
