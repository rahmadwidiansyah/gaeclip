# Solution Matrix: 44 Audit Findings

| # | Audit finding | Solution | Detailed design |
|---:|---|---|---|
| 1 | MVP tidak konsisten | Empat release boundary: CLI, API, CV, UI | `01-scope-and-product.md` |
| 2 | Product constraints tidak terukur | Central config dengan defaults/hard limits | `01-scope-and-product.md` |
| 3 | Copyright/ToS belum dibahas | Rights policy, reject inaccessible content, retention | `01-scope-and-product.md` |
| 4 | Acceptance criteria umum | ffprobe/media/quality acceptance checks | `01-scope-and-product.md` |
| 5 | Transcript schema belum formal | Versioned JSON/Pydantic schema | `02-pipeline-and-data.md` |
| 6 | Line ID belum stabil | Normalize, dedupe, merge, immutable ID algorithm | `02-pipeline-and-data.md` |
| 7 | Transcript LLM tanpa chunking | Token estimate, chunk, shortlist, final rank, cache | `02-pipeline-and-data.md` |
| 8 | Validasi LLM tidak lengkap | Strict schema, reject policy, deterministic fallback | `02-pipeline-and-data.md` |
| 9 | Smart download belum reliable | Format policy, range fallback, post-trim, ffprobe | `02-pipeline-and-data.md` |
| 10 | Buffer timestamp ambigu | Clamp, merge ranges, relative timestamp rules | `02-pipeline-and-data.md` |
| 11 | Whisper failure modes kabur | Subtitle quality gate, explicit model/device/cache | `02-pipeline-and-data.md` |
| 12 | Active-speaker model belum dipilih | Release C baseline + adapter + benchmark/license | `02-pipeline-and-data.md` |
| 13 | Lip movement bukan bukti tunggal | Combine visual, VAD/audio, smoothing, unknown state | `02-pipeline-and-data.md` |
| 14 | Hysteresis terlalu sederhana | Formal deterministic state machine and thresholds | `02-pipeline-and-data.md` |
| 15 | Dynamic crop belum punya rules | Safe margin, clamp, smoothing, fallback layout | `02-pipeline-and-data.md` |
| 16 | Subtitle rendering kabur | Fixed media/style/font/libass contract | `02-pipeline-and-data.md` |
| 17 | API contract draft | Canonical endpoints, status codes, OpenAPI | `03-api-and-jobs.md` |
| 18 | Endpoint naming conflict | `/api/v1/jobs` canonical | `03-api-and-jobs.md` |
| 19 | Job states kurang | Full lifecycle with retry/partial/cancelled/expired | `03-api-and-jobs.md` |
| 20 | Progress undefined | Stage weights, monotonic progress, stage progress | `03-api-and-jobs.md` |
| 21 | Idempotency tidak terhubung | Idempotency-Key and request fingerprint | `03-api-and-jobs.md` |
| 22 | Cancellation/cleanup API hilang | Cancel, delete, cooperative worker cancellation | `03-api-and-jobs.md` |
| 23 | Observability tidak ada contract | Structured fields and metrics | `03-api-and-jobs.md` |
| 24 | Public auth concern | Local-only localhost boundary; auth required if exposed | `04-local-security.md` |
| 25 | URL validation kurang | HTTPS YouTube allowlist, SSRF/private network rejection | `04-local-security.md` |
| 26 | Artifact authorization kurang | Job-scoped allowlist and future signed URL | `03-api-and-jobs.md` |
| 27 | File path hardening kurang | Server IDs, resolve-under-root, atomic writes | `04-local-security.md` |
| 28 | Secret handling kurang | `.env` permissions, redaction, no frontend secret | `04-local-security.md` |
| 29 | Supply chain kurang | Pins, lockfiles, checksums, scans | `04-local-security.md` |
| 30 | Compose tidak buildable | Profiles and honest runnable-image boundary | `05-runtime-and-deployment.md` |
| 31 | Ports tidak tersedia | Local-only explicit loopback ports | `05-runtime-and-deployment.md` |
| 32 | `depends_on` bukan readiness | Healthchecks and readiness checks | `05-runtime-and-deployment.md` |
| 33 | Redis insecure/undurable | Loopback/internal network, TTL, memory policy | `05-runtime-and-deployment.md` |
| 34 | GPU tidak operasional | CPU/GPU profiles and startup validation | `05-runtime-and-deployment.md` |
| 35 | Shared storage limited | Storage adapter and single-node boundary | `05-runtime-and-deployment.md` |
| 36 | Resource limits hilang | Queue, concurrency, memory/disk/time limits | `05-runtime-and-deployment.md` |
| 37 | Health/restart/shutdown hilang | Healthchecks, restart policy, signal handling | `05-runtime-and-deployment.md` |
| 38 | Model/provider claims belum valid | Compatibility matrix and explicit baseline decisions | `07-open-decisions.md` |
| 39 | Cost model hilang | Token/cost metadata, cache, budget, circuit breaker | `01-scope-and-product.md`, `02-pipeline-and-data.md` |
| 40 | Retry policy kurang | Typed failure matrix and idempotent checkpoints | `05-runtime-and-deployment.md` |
| 41 | Retention/GC hilang | TTL, cleanup scheduler, quota, metrics | `05-runtime-and-deployment.md` |
| 42 | Backup/recovery hilang | Atomic checkpoints, orphan scan, production backup gate | `05-runtime-and-deployment.md` |
| 43 | Testing/QA hilang | Unit, contract, integration, media, load, benchmark suites | `06-testing-and-quality.md` |
| 44 | Dokumentasi/version/open decisions tidak lengkap | Config reference, version policy, decision register, implementation order | `07-open-decisions.md` |

## Scope note for finding 24

Finding 24 originally menganggap tidak adanya authentication sebagai masalah
critical untuk public service. Pada keputusan baru, aplikasi memang local-only,
sehingga solusi yang benar bukan menambahkan login user, melainkan memastikan
localhost binding dan fail-safe ketika user mencoba expose service. Auth tetap
menjadi mandatory pada mode non-local.
