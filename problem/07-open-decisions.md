# 7. Open Decisions dan Urutan Implementasi

## Decisions already made

| Decision | Resolution |
| --- | --- |
| Deployment target | Laptop-local, single-user, non-public |
| Auth | Not required while all services bind localhost; required if boundary changes |
| Canonical API | `/api/v1/jobs` |
| Storage MVP | `storage/jobs/{job_id}` with adapter boundary |
| MVP visual AI | Excluded from Release A |
| Queue | Celery + Redis in Release B |
| Source | YouTube allowlist only |
| Retention | Intermediate 6h, final 24h by default |

## Decisions required before Release A

1. Python version and package manager/lockfile.
2. FFmpeg supported version and output profile.
3. Gemini client/API version, model, token budget, and fallback behavior.
4. Transcript line merge thresholds and supported languages.
5. Default download format and partial extraction fallback.
6. Licensed sample media and acceptance fixtures.

## Decisions required before Release C

1. Face detector/tracker baseline and model license.
2. Active speaker model baseline and whether VAD/diarization is included.
3. Confidence calibration dataset and thresholds.
4. Crop geometry, smoothing, and split-screen composition.
5. CPU/GPU hardware matrix and model cache strategy.

## Implementation sequence

1. Add config models and limits.
2. Add transcript/candidate/artifact schemas.
3. Add URL validator and safe job storage.
4. Add subtitle parsers and line normalization.
5. Add fake providers and deterministic tests.
6. Add yt-dlp/FFmpeg adapters with subprocess hardening.
7. Add curation client and strict fallback.
8. Add static renderer and media acceptance checks.
9. Add CLI and document reproducible local command.
10. Add API/Redis/Celery and lifecycle tests.
11. Add active-speaker adapter and labeled benchmark.
12. Add hysteresis/crop renderer.
13. Add frontend.

## Definition of done for each component

- input/output schema documented and versioned;
- invalid input and provider failure mapped to typed errors;
- unit and integration tests exist;
- timeout, resource, and cleanup behavior defined;
- logs contain correlation/job/stage context without secrets;
- README command works in the supported local profile;
- no component silently falls back to a materially different behavior.
