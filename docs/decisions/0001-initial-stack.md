# ADR 0001: Initial application stack

- Status: Accepted for MVP
- Date: 2026-09-14

## Context

Video download, transcription, model inference, and FFmpeg rendering are long-running and resource-intensive. They must not run inside a synchronous web request or a single overloaded container.

## Decision

Use FastAPI for the HTTP API, Celery with Redis for asynchronous work, Python workers for media/AI processing, and Next.js/React for the web UI. Use yt-dlp and FFmpeg for media operations, faster-whisper for local transcription fallback, and Gemini through an environment-provided API key for text curation.

## Consequences

- API and worker can scale or restart independently.
- Redis becomes an operational dependency and must receive resource/retention limits.
- Worker image is heavier and may require separate CPU/GPU variants.
- LLM output must be schema-validated before it controls media download.
- Provider rate limits and model licensing must be validated before production.

## Alternatives

- A single synchronous service is simpler but unsuitable for long-running jobs.
- A managed queue/object storage can replace Redis/local storage when deployment requirements are known.
- LLM provider and active speaker model remain replaceable behind small interfaces.

## Addendum 2026-09-14 (verified from code, no decision change)

- Python: `pyproject.toml` requires `>=3.11,<3.15`; Docker images use `3.12-slim`.
- Download format: `bv*[height<=1080]+ba/b[height<=1080]/b`, merge to mp4
  (`downloader.py`); partial extraction via `--download-sections` with
  FFmpeg post-trim.
- Transcript merge: gap `<=0.25s`, total `<=7s` (`subtitles.py`).
- Curation default model `gemini-2.5-flash` (local `.env` uses
  `gemini-3.5-flash`); URL allowlist additionally includes `m.youtube.com`.
- Deferred (documented gaps): transcript chunking, retention/cleanup,
  correlation metrics, graceful cancel, full `422->409` mapping.
