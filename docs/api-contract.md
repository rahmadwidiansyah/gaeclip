# API Contract

Phase 3 implements this contract with FastAPI, Redis, and Celery. Run the full
stack locally with
`docker compose --profile app --profile frontend up -d --build`
(plain `docker compose up` starts only `redis` because `api`/`worker` use
profile `app` and `frontend` uses profile `frontend` — by design).
Run the API locally with `uvicorn gaeclip.api:create_app --factory`; run the worker with
`celery -A gaeclip.celery_app:app worker`.

## `POST /api/v1/jobs`

Creates an asynchronous generation job. Implemented request fields
(`src/gaeclip/api.py`): `source_url`, `language`. `max_clips`/`profile`
documented here are planned and not yet accepted by the API.

```json
{
  "source_url": "https://www.youtube.com/watch?v=example",
  "language": "id"
}
```

Response: `202 Accepted`. The record is created as `accepted`, then
transitioned to `queued`/`enqueueing` once Celery enqueue succeeds, so clients
will typically read back `status: "queued"`.

```json
{
  "job_id": "job_01...",
  "status": "accepted",
  "stage": "enqueueing",
  "progress": 0
}
```

## `GET /api/v1/jobs/{job_id}`

Returns job status and progress.

```json
{
  "job_id": "job_01...",
  "status": "running",
  "stage": "curating",
  "progress": 35,
  "error": null
}
```

For completed jobs, `clips` contains public metadata and a `download_url`.
Internal filesystem paths are never returned to clients.

## `GET /api/v1/jobs/{job_id}/results`

Returns completed clip metadata and download URLs. Before a terminal successful
state, endpoint returns `409 Conflict`. The canonical lifecycle is
`accepted`, `queued`, `running`, `retrying`, `completed`, `partial`, `failed`,
`cancelled`, and `expired`.

```json
{
  "job_id": "job_01...",
  "clips": [
    {
      "clip_id": "clip_01...",
      "title": "Example hook",
      "duration_seconds": 42.5,
      "download_url": "/api/v1/jobs/job_01.../clips/clip_01..."
    }
  ]
}
```

## Error shape

```json
{
  "error": {
    "code": "INVALID_SOURCE_URL",
    "message": "The source URL is not supported.",
    "retryable": false
  }
}
```

## Additional endpoints

- `POST /api/v1/jobs/{job_id}/cancel` — request cooperative cancellation.
- `DELETE /api/v1/jobs/{job_id}` — delete a terminal job and its artifacts.
- `GET /api/v1/jobs/{job_id}/clips/{clip_id}` — stream one validated artifact.
- `GET /health/live` — process liveness.
- `GET /health/ready` — Redis, storage, and required binary readiness.

`Idempotency-Key` is accepted on `POST /jobs`. A repeated key with the same
normalized request returns the existing job; the same key with a different
request currently returns `422` (`INVALID_INPUT` from `JobRepository.create`
via `api.py`) — target contract is `409`.

Cancellation today transitions straight to `cancelled` (no intermediate
`cancel_requested`); the worker checks the flag at job start and after
generation, not between stages and without subprocess kill.

Not implemented yet: `GET /health/config`, stage weights/`ETA`,
`correlation_id`/metrics. Worker progress today is `10` (running) then `100`
(completed).

For the local profile, authentication is intentionally omitted because the API
must bind only to `127.0.0.1`. Enabling `PUBLIC_MODE=true` without authentication
is invalid and must prevent startup.
