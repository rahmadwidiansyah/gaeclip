# Gaeclip Desktop Architecture (dual runtime)

GAEClip Core is shared. Two runtimes, one pipeline:

```
                    GAEClip Core (pipeline, curation, framing, TalkNet, render)
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
           SERVER RUNTIME                 DESKTOP RUNTIME
     GAECLIP_RUNTIME=server       GAECLIP_RUNTIME=desktop
     (default; Docker Compose)    (Tauri installer)
                    │                               │
        Redis + Celery + Docker        SQLite + LocalWorker + Tauri
```

Docker remains fully supported and is the default. Nothing below
changes server behavior; desktop-only code lives in
`src/gaeclip/runtime/`, `src/gaeclip/desktop.py`, `src-tauri/`.

## Shared core (never forked)

Download, yt-dlp, transcription/Whisper, curation + Gemini pool +
fallback hierarchy, heatmap, heuristic, deterministic, artifact reuse,
recreate, auto-framing, TalkNet, subtitles, rendering, QC, job/progress
model, error classification. Application code talks to the store-client
contract (`runtime.abc`), never to Redis/Celery directly.

## Runtime selection

`GAECLIP_RUNTIME` unset/blank → `server`. Unknown values raise
`ConfigurationError` instead of guessing. Desktop ignores `REDIS_URL`
with a warning, so it can never join an external Redis by accident.

## Desktop components

- **Store**: `SQLiteClient` (`STORAGE_ROOT/gaeclip.db`: kv/hashes/sets,
  `nx`/`ex`/expiry/scan/pipeline/counters). All existing stores
  (jobs, settings, transcripts, videos, providers, health) run on it
  unchanged.
- **Runner**: `LocalJobRunner` (in-process FIFO threads, `process_job`
  reuse, cancel via record status + pre-start drop). Same submit contract
  as Celery (`job_id -> task_id`).
- **Secrets**: process `SETTINGS_SECRET` from env, DPAPI blob
  (`%LOCALAPPDATA%/Gaeclip/.settings_secret`), legacy key file, or fresh
  generation — in that order. Server path untouched.
- **Cookies**: `YTDLP_COOKIES_FILE` env still wins everywhere; otherwise
  `<STORAGE_ROOT>/cookies.txt` (Settings → YouTube → Cookies writes it).
- **Frontend**: same Next.js UI; System page shows runtime + SQLite
  instead of Redis on desktop; YouTube Cookies card added (both editions
  may use it).
- **Launcher**: `python -m gaeclip.desktop` (uvicorn in-process) as the
  Tauri `gaeclip-api` sidecar; Next.js standalone as the second sidecar.
- **Installer**: NSIS via `src-tauri/` (icons + signing keys required;
  see `src-tauri/icons/README.md`). Bundled: Python+deps, torch CPU,
  Node, ffmpeg, Deno, models, fonts.

## First run (desktop)

Install → Open → Settings: paste Gemini key(s) and/or YouTube cookies
→ Ready. No Docker/Python/Node/Redis/Celery/FFmpeg to install manually.

## Validation matrix

- `pytest tests/` (both backends via `test_runtime.py` parity suite).
- `docker compose build` + server suite (Docker untouched).
- Windows CI: installer build, desktop smoke (`/health/config`
  reports `runtime=desktop`), cancel/recreate flows.
