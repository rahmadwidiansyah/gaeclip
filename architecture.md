# Architecture

## Overview

Gaeclip menggunakan asynchronous service architecture. API menerima request singkat dan membuat job; worker mengerjakan download, transcript, AI curation, visual analysis, dan rendering tanpa menahan HTTP request.

```text
Browser
  |
  v
Next.js frontend ---> FastAPI API ---> Redis broker/result state ---> Celery worker
                                                                    |
                                                                    +--> yt-dlp / FFmpeg
                                                                    +--> Whisper / LLM
                                                                    +--> Face and speaker models
                                                                    |
                                                                    v
                                                              Shared artifact storage
```

## Components

### Frontend

Next.js/React menyediakan URL form, job progress, error state, clip preview, dan download links. Frontend tidak menjalankan media processing.

### API

FastAPI melakukan input validation, membuat `job_id`, enqueue task, membaca status, dan mengembalikan artifact metadata. API tidak menjalankan FFmpeg atau model inference di request handler.

### Queue and state

Redis dipakai sebagai Celery broker dan MVP job state backend. Lifecycle
canonical yang dipakai semua service (`src/gaeclip/jobs.py`) adalah:
`accepted`, `queued`, `running`, `retrying`, `completed`, `partial`, `failed`,
`cancelled`, dan `expired`. `processing` hanya alias legacy dan tidak boleh
muncul dalam response baru. Worker saat ini memakai subset
`running/completed/failed/cancelled`; `retrying/partial/expired` tersedia di
schema tetapi belum ditransisikan oleh worker.

### Worker

Celery worker menjalankan pipeline Python. Worker memiliki akses ke FFmpeg, yt-dlp, transcription model, LLM client, computer vision model, dan shared storage.

### Storage

Setiap job memiliki directory terisolasi, misalnya `storage/jobs/{job_id}/`, untuk metadata, subtitle, intermediate clips, logs, dan final artifacts. Generated media tidak di-commit ke repository.

## Processing flow

1. API memvalidasi URL dan membuat job.
2. Worker membaca metadata video dan mencoba mengambil subtitle.
3. Jika subtitle tidak tersedia, worker mengekstrak audio dan menjalankan faster-whisper.
4. Transcript diberi line ID dan dikirim ke LLM untuk candidate selection.
5. Worker mengunduh rentang candidate clip dengan buffer.
6. Visual analysis dijalankan hanya pada candidate clip.
7. Timeline speaker dan layout dibuat dengan hysteresis minimum 2 detik.
8. FFmpeg merender vertical video dan subtitle.
9. Artifact metadata disimpan; job ditandai `completed` bila semua requested clips
berhasil, atau `partial` bila sebagian clip berhasil.

## Boundaries and reliability

- HTTP timeout tidak mempengaruhi pekerjaan worker.
- Task harus idempotent terhadap `job_id` dan tidak menimpa artifact job lain.
- External command harus memakai argument list tervalidasi, bukan shell string yang dibentuk dari input user.
- Setiap stage menulis progress dan error context.
- Retry hanya diterapkan pada operasi transient; invalid URL dan invalid model output harus fail fast.

## Initial decisions

- FastAPI + Celery/Redis dipilih untuk memisahkan request lifecycle dari long-running media work.
- Candidate-first processing membatasi computer vision pada clip pendek.
- Local shared storage dipakai untuk MVP dengan interface yang dapat diganti object storage.
- Deployment default hanya localhost/single-user. Authentication tidak dipakai
  selama `PUBLIC_MODE=false`; public/non-local mode wajib menolak startup tanpa
  authentication dan rate limiting.
