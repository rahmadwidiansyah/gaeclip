# Configuration Reference

Configuration is loaded from `.env` for local development and must be validated
before starting API/worker. `.env.example` contains names and local defaults; it
does not contain secrets.

| Variable | Type | Default | Service | Secret |
| --- | --- | --- | --- | --- |
| `GEMINI_API_KEY` | string | empty | worker | yes |
| `REDIS_URL` | URL | `redis://redis:6379/0` | api/worker | no |
| `STORAGE_ROOT` | path | `/app/storage` | api/worker | no |
| `API_HOST` | host | `127.0.0.1` local / `0.0.0.0` container | api | no |
| `API_PORT` | integer | `8000` | api | no |
| `NEXT_PUBLIC_API_BASE_URL` | URL | `http://127.0.0.1:8000` | frontend | no |
| `FRONTEND_HOST` | host | `127.0.0.1` | frontend | no |
| `FRONTEND_PORT` | integer | `3000` | frontend | no |
| `WORKER_CONCURRENCY` | integer | `1` | worker | no |
| `MAX_SOURCE_DURATION_SECONDS` | integer | `7200` | api/worker | no |
| `MAX_CLIPS` | integer | `3` | api/worker | no |
| `MIN_CLIP_DURATION_SECONDS` | integer | `30` | api/worker | no |
| `MAX_CLIP_DURATION_SECONDS` | integer | `60` | api/worker | no |
| `CLIP_BUFFER_SECONDS` | float | `1.5` | worker | no |
| `MAX_TRANSCRIPT_CHARS` | integer | `400000` | worker | no |
| `MAX_ARTIFACT_BYTES` | integer | `524288000` | worker | no |
| `MAX_JOB_SECONDS` | integer | `3600` | worker | no |
| `MAX_LLM_OUTPUT_TOKENS` | integer | `4096` | worker | no |
| `MAX_CONCURRENT_JOBS` | integer | `1` | api/worker | no |
| `ARTIFACT_RETENTION_HOURS` | integer | `24` | worker | no |
| `INTERMEDIATE_RETENTION_HOURS` | integer | `6` | worker | no |
| `PUBLIC_MODE` | boolean | `false` | api | no |
| `GEMINI_MODEL` | string | `gemini-3.8-flash` | worker | no |
| `GEMINI_TIMEOUT_SECONDS` | integer | `30` | worker | no |
| `AI_MODEL_MODE` | enum (`automatic`/`manual`) | `automatic` | worker | no |
| `AI_PRIMARY_MODEL` | string (model id, empty = legacy `GEMINI_MODEL`) | `` | worker | no |
| `AI_FALLBACK_MODELS` | string (JSON list of model ids) | `[]` | worker | no |
| `AI_FREE_TIER_ONLY` | boolean | `true` | worker | no |
| `MODEL_DISCOVERY_CACHE_TTL` | integer (seconds, env-only) | `21600` | api | no |
| `RENDER_PROFILE` | enum (`fast`/`balanced`/`quality`) | `quality` | worker | no |
| `WHISPER_MODEL` | string | `small` | worker | no |
| `TRANSCRIPT_SOURCE` | enum (`youtube_first`/`whisper_only`/`youtube_only`) | `youtube_first` | worker | no |

Optional visual dependencies are installed with the `vision` extra. They are
deliberately not part of the base lock because PyTorch wheels differ by CPU,
CUDA, and operating system. See [visual-models](visual-models.md).
`requirements.lock` is currently minimal (pydantic stack only) and does not
yet pin `yt-dlp`/API/frontend dependencies.

## Implementation status (2026-09-14)

- Enforced by `Settings`: `MAX_*` limits, `MIN<MAX`, positive values,
  `CLIP_BUFFER_SECONDS` 0–5, `PUBLIC_MODE=true` requires non-loopback host.
- Documented but not enforced: `ARTIFACT/INTERMEDIATE_RETENTION_HOURS`
  (no cleanup scheduler; Redis job keys have no TTL — only idempotency keys
  expire after 24h), `WORKER_CONCURRENCY` (Celery CLI flag, not `Settings`),
  `FRONTEND_HOST/PORT`, `LOG_LEVEL`, `REDIS_URL` (read via `os.getenv`
  directly).
- Local `.env` drift: `GEMINI_MODEL` is `gemini-3.5-flash` locally vs default
  `gemini-2.5-flash`; legacy `FRONTEND_ORIGIN` (singular) vs canonical
  `FRONTEND_ORIGINS` (plural, now present).

## Validation rules

- `PUBLIC_MODE=false` requires API, frontend, and Redis host bindings to remain
  loopback/local. The application must reject a public bind without auth.
- `MIN_CLIP_DURATION_SECONDS < MAX_CLIP_DURATION_SECONDS`.
- `MAX_CLIPS` and `MAX_CONCURRENT_JOBS` must be positive.
- retention values and timeouts must be positive.
- `STORAGE_ROOT` must be writable and have sufficient free space.
- `GEMINI_API_KEY` is required only when the selected curation provider is Gemini;
  mock/deterministic curation is allowed for tests.
- If Gemini times out or returns an invalid/error response, curation falls back
  automatically. When YouTube exposes an audience heatmap, fallback windows
  are ranked by its highest-intensity timestamps.
- Secrets must not be printed in startup logs.

The browser uses `NEXT_PUBLIC_API_BASE_URL`; `redis` and `api` Docker hostnames
are internal container DNS names and must never be put into a browser-facing
variable.
