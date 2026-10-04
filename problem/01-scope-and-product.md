# 1. Scope dan Product Rules

## Release boundaries

### Release A: local CLI MVP

In-scope: URL YouTube, metadata, subtitle download, faster-whisper fallback,
normalized transcript, deterministic candidate selection via LLM, partial media
download, static center crop, SRT/ASS subtitle, final MP4, structured logs, dan
unit/integration tests.

Out-of-scope: Celery, web frontend, active speaker, GPU, multi-user, object
storage, dan public deployment. CLI harus tetap dapat memakai mock LLM untuk test.

### Release B: local API runtime

Tambahkan FastAPI, Redis, Celery, job status, cancellation, artifact endpoint,
local Docker Compose, healthchecks, cleanup task, dan polling frontend contract.

### Release C: visual intelligence

Tambahkan face tracking, active speaker baseline, visual/audio confidence,
hysteresis state machine, dynamic crop, split-screen, dan quality benchmark.

### Release D: UI

Tambahkan Next.js/React submit form, progress polling, clip list, preview, dan
download. UI bukan dependency untuk memvalidasi core pipeline.

## Product limits

Simpan limits terpusat dalam `config` dan validasi sebelum enqueue:

| Key | Default | Hard limit |
| --- | ---: | ---: |
| `MAX_SOURCE_DURATION_SECONDS` | 7200 | 21600 |
| `MAX_CLIPS` | 3 | 5 |
| `MIN_CLIP_DURATION_SECONDS` | 15 | 10 |
| `MAX_CLIP_DURATION_SECONDS` | 90 | 180 |
| `CLIP_BUFFER_SECONDS` | 1.5 | 5 |
| `MAX_TRANSCRIPT_CHARS` | 400000 | 1000000 |
| `MAX_ARTIFACT_BYTES` | 524288000 | 2147483648 |
| `MAX_JOB_SECONDS` | 3600 | 10800 |
| `MAX_LLM_OUTPUT_TOKENS` | 4096 | 8192 |
| `MAX_CONCURRENT_JOBS` | 1 | 2 |

Reject input yang melampaui hard limit dengan error non-retryable. Values harus
ditampilkan pada `GET /health/config` hanya sebagai non-secret effective limits.

## Content and rights policy

Aplikasi hanya memproses URL yang user berhak unduh dan olah. Private,
age-restricted, geo-restricted, atau inaccessible content gagal dengan error
yang jelas; jangan mencoba bypass access control. Artifact lokal dihapus sesuai
retention. Public launch memerlukan legal/ToS review terpisah.

## Acceptance criteria

Setiap final MP4 harus memenuhi:

- container MP4, video H.264, audio AAC bila input memiliki audio;
- aspect ratio target 9:16, default 1080x1920 atau configured fallback;
- constant frame rate 30 fps atau source-preserving policy yang terdokumentasi;
- audio/video duration difference <= 100 ms;
- subtitle tidak keluar safe area dan tidak overlap frame secara tidak terbaca;
- clip start/end berada di dalam source duration;
- hasil `ffprobe` valid dan file dapat decode dari awal sampai akhir;
- metadata job berisi source, config version, model versions, dan artifact checksum.

## Change control

Perubahan limits, status, schema, model, atau output media memerlukan ADR kecil dan
fixture test. Jangan mengubah behavior hanya dari prompt atau environment tanpa
menaikkan `schema_version`/`pipeline_version`.
