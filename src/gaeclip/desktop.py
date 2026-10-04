"""Desktop edition launcher: ``python -m gaeclip.desktop``.

Starts the local FastAPI + in-process worker (GAECLIP_RUNTIME=desktop,
SQLite store, LocalRunner) on loopback. The Tauri shell spawns this as a
sidecar and loads the bundled frontend; developers can also run it
directly. Server/Docker deployments never import this module.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("GAECLIP_RUNTIME", "desktop")
    from .runtime.cookies import export_cookies_file
    from .runtime.secrets import ensure_desktop_secret

    try:
        ensure_desktop_secret()
    except Exception as exc:  # never block startup on secrets bootstrap
        print(f"warning: desktop secret bootstrap failed: {exc}", file=sys.stderr)
    export_cookies_file()

    host = os.getenv("API_HOST", "127.0.0.1")
    try:
        port = int(os.getenv("API_PORT", "8000"))
    except ValueError:
        port = 8000
    try:
        import uvicorn
    except ImportError:
        print("error: uvicorn is not installed (bundle the api extra)",
              file=sys.stderr)
        return 2
    print(f"Gaeclip Desktop starting on http://{host}:{port} ...")
    uvicorn.run("gaeclip.api:create_app", factory=True, host=host, port=port,
                log_level=os.getenv("LOG_LEVEL", "info").lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
