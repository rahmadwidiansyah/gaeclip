# Gaeclip

Auto-Shorts Generator — turn YouTube videos into vertical short clips with AI-assisted curation, active-speaker framing, and burned-in subtitles.

> **Just want to use Gaeclip?**
>
> - **On Windows, no Docker wanted?** → Install the **Windows Desktop app** ([see below](#-windows-desktop)). No Docker, Python, Redis, or FFmpeg to install manually.
> - **Already use Docker?** → Pull the GHCR images and start with Compose ([see below](#-docker)). No build needed.
> - **Developer?** → Clone, `docker compose up -d --build`, read [Development](#-development).

One codebase, two ways to run it. Both editions share the same Gaeclip Core
(pipeline, curation + fallbacks, framing, rendering); they differ only in how
jobs are stored and executed (Redis + Celery on server, SQLite + in-process
runner on desktop). See [docs/desktop-architecture.md](docs/desktop-architecture.md).

## Features

- Automatic clipping of YouTube videos to 9:16 vertical MP4
- AI-assisted curation (Gemini provider pool with priority/fallback routing)
- Graceful fallback chain: Gemini → YouTube audience heatmap → transcript heuristic → deterministic windows
- Automatic vertical framing with active-speaker follow (MediaPipe/YOLO/TalkNet escalation)
- Subtitle burn-in (ASS styling, configurable style/position)
- Multi-provider Gemini keys, model discovery, connection testing, per-provider health
- YouTube login cookies support (for bot-check / 429 regions)
- Job API: idempotency keys, cancellation, recreate, progress polling, artifact download
- Docker deployment (GHCR images) and Windows desktop application

## Quick Start

### Windows

1. Download the installer from
   [GitHub Releases](https://github.com/rahmadwidiansyah/gaeclip/releases)
   (`Gaeclip_<version>_x64-setup.exe`; fresh builds also live under the
   `windows-desktop` workflow Artifacts as `gaeclip-setup`).
2. Install → open Gaeclip.
3. Settings → paste your Gemini API key (get one at Google AI Studio).
4. Optional: Settings → YouTube → paste login cookies if YouTube shows bot checks.
5. Paste a YouTube URL → get clips.

Everything is bundled: Python backend, Node.js frontend server, FFmpeg/Deno,
ML models. Nothing else to install.

### Docker

Requirements: Docker Engine 24+ with Compose v2, ~4 GB free disk for images.

```bash
git clone https://github.com/rahmadwidiansyah/gaeclip.git
cd gaeclip
cp .env.example .env   # fill in GEMINI_API_KEY (or set it later in the UI)
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Open http://127.0.0.1:3000. The API is at http://127.0.0.1:8000.

To run a pinned release instead of `latest`:

```bash
GAECLIP_VERSION=1.2.3 docker compose -f docker-compose.prod.yml pull
GAECLIP_VERSION=1.2.3 docker compose -f docker-compose.prod.yml up -d
```

## Requirements

### Windows

- Windows 10 21H2+ / 11, x64
- WebView2 (installed automatically by the installer if missing)
- Internet access (YouTube, Gemini API)
- Optional: a Google AI Studio API key for AI curation (works without it via fallbacks)

### Docker

- Docker Engine 24+ and Compose v2
- x86_64 host (`linux/amd64` images; see [Architecture](#-architecture) for why ARM64 is not shipped)
- ~4 GB disk for images, plus room for video storage (`./storage` bind mount)
- Internet access (YouTube, Gemini API)

## Configuration

All settings live in `.env` (copy from `.env.example`). Two kinds:

1. **Infrastructure** (read at startup, needs restart): `REDIS_URL`, `STORAGE_ROOT`,
   ports, `FRONTEND_ORIGINS`, `SETTINGS_SECRET`, `PUBLIC_MODE`, `API_AUTH_TOKEN`.
   A public bind (`PUBLIC_MODE=true`, e.g. behind Cloudflare Tunnel) refuses to
   start without `API_AUTH_TOKEN`; enter the same token under
   Settings → Koneksi API.
2. **Application seed** (first boot only, afterwards the database wins — prefer the
   Settings UI): `GEMINI_API_KEY`, `GEMINI_MODEL`, clip durations, worker tuning.

Full reference: [docs/configuration.md](docs/configuration.md).

## Gemini Providers

Settings → Gemini Providers supports **multiple API keys** with priority,
per-model fallback chains, enable/disable, health tracking, connection testing,
and live model discovery. If all providers fail, the curation fallback chain
keeps jobs completing (heatmap → heuristic → deterministic).

## YouTube Cookies

If YouTube answers 429 or *"Sign in to confirm you're not a bot"*:

1. Export a Netscape `cookies.txt` from a logged-in browser session.
2. Settings → YouTube → Cookies → paste the file content (desktop), or set
   `YTDLP_COOKIES_FILE=/app/storage/cookies.txt` (Docker, file mounted there).

Cookies never leave your machine except toward YouTube itself.

## Storage

- Docker: video artifacts live in `./storage` (bind mount, survives updates);
  Redis state in the `redis-data` volume.
- Desktop: `%LOCALAPPDATA%\Gaeclip\storage` (exact data dir varies by install).

## Docker

Images are published automatically to GHCR on every `main` push (after tests
pass) and on version tags. Pull — don't build — for normal use.

One **mono image** holds the whole server edition (FastAPI backend + Celery
worker + Next.js UI supervised in one container); Redis runs as its companion
service:

| Image | Content |
|---|---|
| `ghcr.io/rahmadwidiansyah/gaeclip` | API + worker + frontend (mono) |

Tags: `latest` (main branch), `1.2.3` / `1.2` / `1` (git tags `v*`), `sha-<commit>`.

Update to a newer release without losing data:

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

(`./storage` and `redis-data` persist across updates.)

Logs:

```bash
docker compose -f docker-compose.prod.yml logs -f       # everything
docker compose -f docker-compose.prod.yml logs -f app    # app service
docker compose -f docker-compose.prod.yml logs -f redis # redis service
```

Tear down (keeps data); add `-v` to also drop the Redis volume:

```bash
docker compose -f docker-compose.prod.yml down
```

### Development with Docker

`docker-compose.yml` (dev) builds all images from source instead of pulling:

```bash
docker compose up -d --build
```

Ports are loopback-bound (`127.0.0.1:8000`, `:3000`, `:6379`).

## Windows Desktop

Status: ✅ **Available** — built by the `windows-desktop` GitHub workflow
(PyInstaller backend + Next.js standalone + Tauri NSIS installer) and
smoke-tested (`runtime=desktop`, no Docker/Redis needed).

First run: Install → Open → Settings → paste Gemini key(s) and/or YouTube
cookies → Ready. See [docs/desktop-architecture.md](docs/desktop-architecture.md)
for the dual-runtime design (SQLite + local runner, DPAPI secret storage).

> Unsigned-installer notice: releases are not Authenticode-signed yet, so
> Windows SmartScreen may ask for confirmation on first install, and Defender
> may flag freshly-downloaded helper binaries. This is expected for unsigned
> software; verify the download source (this repo's Actions/Releases) before
> allowing it.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[api,dev]"
.venv/bin/python -m pytest tests/ -q
```

```bash
cd frontend && npm install && npm run dev   # UI on :3000
```

```bash
uvicorn gaeclip.api:create_app --factory --host 127.0.0.1 --port 8000
celery -A gaeclip.celery_app:app worker --loglevel=INFO --concurrency=1
```

Backend deps per area: `.[llm]` (Gemini), `.[transcription]` (faster-whisper),
`.[framing]` / `.[vision]` (MediaPipe etc.). ROCm notes live in
[docs/visual-models.md](docs/visual-models.md).

## Testing

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_vision_benchmark.py
cd frontend && npm test
```

CI runs the same suites: `docker.yml` (quality gate before any GHCR push) and
`windows-desktop.yml` (installer build + desktop smoke test).

## Architecture

- [goals.md](goals.md), [architecture.md](architecture.md), [roadmap.md](roadmap.md)
- [docs/pipeline.md](docs/pipeline.md), [docs/api-contract.md](docs/api-contract.md),
  [docs/configuration.md](docs/configuration.md)
- [docs/desktop-architecture.md](docs/desktop-architecture.md) (dual runtime),
  [docs/visual-models.md](docs/visual-models.md) (detectors, ROCm notes)

Platform note: Docker images ship `linux/amd64` only. The ML stack has no
trustworthy ARM64 path (torch CPU index is x86_64-focused, MediaPipe publishes
no `linux/aarch64` wheels, and QEMU-emulated ARM builds of this stack are
impractically slow and fragile), so ARM64 is deliberately not offered.

## Troubleshooting

| Symptom | Likely cause → fix |
|---|---|
| Job fails at metadata with `yt-dlp` error | No network to YouTube, or bot-check → add YouTube cookies (see above) |
| `Sign in to confirm you're not a bot` / 429 | YouTube cookies required (see above) |
| Clips render but framing is static center crop | Vision model asset missing/unreachable; check worker logs for `Active Speaker Detection unavailable` |
| `ffmpeg not found` (local CLI) | Install FFmpeg on PATH (Docker/desktop bundle it) |
| Desktop app closes instantly on launch | Check `%LOCALAPPDATA%\<app-id>\Gaeclip\desktop-startup.log`; ensure ports 3000/8000 are free |
| Windows flags the installer/binaries | Expected for unsigned builds (see notice above); verify source, allow, or add an exclusion for the install folder |
| Redis connection refused (local dev) | Start it: `docker compose up redis` |

## Releases

- Docker: `ghcr.io/rahmadwidiansyah/gaeclip` —
  `latest` tracks `main`; pin `GAECLIP_VERSION=1.2.3` for stable. Versioning: `vMAJOR.MINOR.PATCH`.
- Windows: NSIS installer from the `windows-desktop` workflow artifacts;
  versioned binaries will be attached to GitHub Releases.

## License

MIT — see [LICENSE](LICENSE).
