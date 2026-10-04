"""Desktop edition launcher: ``python -m gaeclip.desktop``.

Starts the local FastAPI + in-process worker (GAECLIP_RUNTIME=desktop,
SQLite store, LocalRunner) on loopback. The Tauri shell spawns this as a
sidecar and loads the bundled frontend; developers can also run it
directly. Server/Docker deployments never import this module.
"""

from __future__ import annotations

import os
import sys


def _ensure_bundled_tools_on_path() -> str | None:
    """Prepend the bundled ``bin/`` dir to PATH when frozen.

    The PyInstaller bundle has no redistributable Scripts/ dir, so bare
    tool names (``yt-dlp``, ``ffmpeg``, ``ffprobe``, ``deno``) would all
    fail with FileNotFoundError. Every call site resolves tools via
    PATH, so one prepend here fixes them all (dev/docker: no-op, tools
    are already on PATH there).
    Returns the prepended dir, or None when not applicable.
    """
    import sys
    from pathlib import Path

    if not getattr(sys, "frozen", False):
        return None
    exe_dir = Path(sys.executable).resolve().parent
    for candidate in (exe_dir / "bin", exe_dir):
        if candidate.is_dir():
            os.environ["PATH"] = str(candidate) + os.pathsep + os.environ.get("PATH", "")
            print(f"Gaeclip Desktop tools dir: {candidate}", file=sys.stderr)
            return str(candidate)
    print("warning: no bundled tools dir found next to executable",
          file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("GAECLIP_RUNTIME", "desktop")
    _ensure_bundled_tools_on_path()
    # Absolute imports: PyInstaller runs this file as a top-level script
    # (__main__), where relative imports fail. Works identically under
    # `python -m gaeclip.desktop`.
    from gaeclip.runtime.cookies import export_cookies_file
    from gaeclip.runtime.secrets import ensure_desktop_secret

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
