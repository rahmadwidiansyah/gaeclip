# Goals

## Product vision

Menyediakan cara cepat untuk mengubah video panjang menjadi short-form content yang relevan, mudah dibaca, dan memiliki framing kamera yang mengikuti pembicara aktif.

## Release A goals: local CLI

- Menerima URL video YouTube yang valid.
- Mengambil metadata dan subtitle dengan `yt-dlp`.
- Menggunakan `faster-whisper` sebagai fallback ketika subtitle tidak tersedia.
- Menghasilkan transcript dengan timestamp dan line ID yang stabil.
- Meminta LLM memilih 3-5 candidate clips dalam structured JSON.
- Mengunduh hanya rentang video candidate, bukan seluruh video panjang.
- Menghasilkan MP4 vertical 9:16 dengan subtitle burn-in.

Release A tidak mencakup API, Celery, Redis, frontend, atau active speaker.

## Release B goals: local API and queue

- Menjalankan pekerjaan berat secara asynchronous melalui Celery dan Redis.
- Menyediakan canonical job lifecycle dan link artifact melalui API.
- Menyediakan cancellation, cleanup, limits, healthcheck, dan retry policy.

## Next goals

- Face tracking dan active speaker scoring pada candidate clips.
- Camera switching dengan hysteresis minimum 2 detik.
- Split-screen ketika beberapa speaker aktif bersamaan.
- Word-level subtitle styling dan per-word highlight.
- Web UI untuk submit URL, melihat progress, dan mengunduh hasil.

## Non-goals for the initial release

- User authentication, billing, dan multi-tenant account management.
- Memproses seluruh video dengan computer vision sebelum clip curation.
- Production-grade object storage, CDN, autoscaling, dan GPU scheduling.
- Menjamin skor viral sebagai ukuran performa bisnis.

## Success criteria

- Satu URL dapat diproses menjadi setidaknya satu MP4 yang dapat diputar.
- Setiap job mengikuti lifecycle canonical dari `accepted` sampai terminal state:
  `completed`, `partial`, `failed`, `cancelled`, atau `expired`.
- Candidate clip timestamps berasal dari transcript/LLM output yang tervalidasi.
- Artifact dan temporary files terisolasi per job.
- Kegagalan downloader, transcription, LLM, atau rendering terlihat sebagai error job yang actionable.

## Constraints and assumptions

- Processing dilakukan offline di worker kecuali pemanggilan LLM Gemini.
- API key disediakan melalui environment variable, bukan source code.
- Local shared storage dipakai untuk MVP; abstraction storage disiapkan untuk migrasi ke S3-compatible storage.
- Model weights, GPU availability, dan biaya/rate limit provider harus divalidasi sebelum production.
