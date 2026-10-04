# Gaeclip

Auto-Shorts Generator yang mengubah video YouTube menjadi short vertical clips dengan bantuan AI.

## Project goals

Gaeclip mengambil subtitle atau audio dari video YouTube, memilih bagian yang berpotensi viral, lalu membuat clip 9:16 dengan subtitle dan camera framing yang mengikuti active speaker. Lihat [goals.md](goals.md) dan [architecture.md](architecture.md).

## Initial scope

Versi awal memprioritaskan pipeline asynchronous dan CLI/API MVP. User memasukkan URL video, sistem membuat job, memilih candidate clips, merender MP4, lalu menyediakan artifact hasil. User authentication belum termasuk scope awal.

## Repository map

| Path | Responsibility |
| --- | --- |
| `api/` | FastAPI application dan job endpoints |
| `worker/` | Celery tasks, media processing, dan AI pipeline |
| `frontend/` | Next.js/React UI |
| `shared/` | Kontrak data dan utility lintas service |
| `infra/` | Deployment dan infrastructure configuration |
| `docs/` | Pipeline, API contract, dan architecture decisions |
| `storage/` | Local generated artifacts, ignored by Git |

## Current status

**Release A CLI selesai sebagai baseline lokal.** Pipeline dapat mengambil
metadata, subtitle atau audio YouTube, memakai `faster-whisper` sebagai fallback,
memilih candidate lewat Gemini atau deterministic fallback, mengunduh hanya
rentang candidate, lalu merender clip MP4 vertikal dengan subtitle burn-in.
API, worker, dan frontend tersedia sebagai Release B baseline. Frontend
menggunakan Next.js dengan token Material 3, polling status job, progress,
error state, dan download artifact. Baseline visual (Release C: adapter
MediaPipe/YOLO/TorchScript + timeline hysteresis + dynamic crop/split) dan
baseline UI (Release D) juga tersedia; kalibrasi, preview video, tombol cancel,
retention/cleanup, dan observability masih tersisa — lihat `roadmap.md` dan
`FINAL-AUDIT.md` bagian Verifikasi Ulang 2026-09-14 untuk Known gaps.

Fase 3 API/worker sekarang tersedia melalui optional extra:

```bash
.venv/bin/pip install -e ".[api]"
uvicorn gaeclip.api:create_app --factory --host 127.0.0.1 --port 8000
celery -A gaeclip.celery_app:app worker --loglevel=INFO --concurrency=1
```

Jalankan Redis lebih dulu dengan `docker compose up redis`. Endpoint job berada
di `/api/v1/jobs`, dengan idempotency key, cancellation, result download, dan
health checks.

## Local setup

1. Copy `.env.example` menjadi `.env` dan isi API key yang diperlukan.
2. Jalankan seluruh stack (API, worker, Redis, frontend) dengan
   `docker compose up -d --build`.
   Di dalam container, API listen `0.0.0.0:8000`, tetapi publish ke host
   tetap loopback (`127.0.0.1:8000`), begitu pula `3000` dan `6379`.
3. Release A CLI dapat dipasang dengan `python -m pip install -e .`; paket ini
   memasang `yt-dlp`, sedangkan `ffmpeg` tetap harus tersedia di PATH.
4. Implementasi berikutnya mengikuti [roadmap.md](roadmap.md).

Contoh menjalankan pipeline Release A setelah instalasi package selesai dan
`ffmpeg` tersedia:

```bash
PYTHONPATH=src python -m gaeclip generate "https://www.youtube.com/watch?v=VIDEO_ID"
```

Output JSON berisi `job_id`, sumber video, dan path clip MP4 di dalam
`STORAGE_ROOT`. Jika video tidak menyediakan subtitle, pipeline mengunduh audio
dan memakai `faster-whisper` bila dependency `gaeclip[transcription]` tersedia.
Jika `GEMINI_API_KEY` diisi, Gemini digunakan untuk kurasi; jika tidak tersedia
atau responsnya tidak valid, deterministic fallback tetap digunakan.

Frontend lokal dapat dijalankan dengan:

```bash
cd frontend
npm install
npm run dev
```

Frontend mengarah ke `NEXT_PUBLIC_API_BASE_URL` dan API mengizinkan origin
`127.0.0.1:3000` serta `localhost:3000` melalui `FRONTEND_ORIGINS`.
Transcript besar otomatis memakai deterministic curation agar tidak mengirim
input berlebihan ke Gemini. Batas input Gemini dapat diatur dengan
`MAX_LLM_INPUT_CHARS` (default `120000`).

Fase 2 menyediakan engine visual adapter-neutral. Detector/model eksternal dapat
menulis observation JSON, lalu timeline hysteresis dan crop portrait dapat diuji:

```bash
PYTHONPATH=src python -m gaeclip analyze-visual observations.json --duration 60
```

Untuk memakai hasil analisis itu saat generate clip:

```bash
PYTHONPATH=src python -m gaeclip generate \
  "https://www.youtube.com/watch?v=VIDEO_ID" \
  --observations observations.json
```

## Real visual models and benchmark

Install the optional detector stack in an isolated environment:

For CPU/OpenCL-only work:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[vision]"
```

For Radeon GPU inference, install a ROCm PyTorch wheel first; the regular PyPI
PyTorch package is not a ROCm build:

```bash
python -m venv .venv-rocm
.venv-rocm/bin/pip install torch==2.9.1 torchvision \
  --index-url https://download.pytorch.org/whl/rocm6.3
.venv-rocm/bin/pip install -e ".[vision]"
```

MediaPipe is the lightweight CPU detector. A YOLO face checkpoint is installed
at `models/yolo26n-face.pt` (SHA-256
`c6a5405127a2e351292315a6a8084ea3e790dbec25b9d16a8e80d1e3f866efe1`):

```bash
.venv/bin/python -m gaeclip benchmark-vision --detector mediapipe --frames 200
.venv/bin/python -m gaeclip benchmark-vision --detector yolo \
  --weights models/yolo26n-face.pt --device auto --frames 200
```

The benchmark reports elapsed time, detections, selected device, and FPS. For
Radeon, run it with `--device cuda:0` only after installing a ROCm PyTorch
wheel; PyTorch exposes ROCm through the `torch.cuda` API. `auto` selects that
backend only when `torch.version.hip` and accelerator availability are both
present.

On this Radeon Renoir host, ROCm PyTorch is installed in `.venv-rocm`
(`torch==2.9.1+rocm6.3`) and detects one AMD device, but YOLO inference caused
a ROCm `GPU Hang`. No GPU FPS is therefore claimed. Use the CPU environment
until a driver/ROCm combination officially supporting this GPU is available;
details are recorded in [docs/visual-models.md](docs/visual-models.md).

TalkNet-ASD and SyncNet source repositories are installed under `models/`.
Their pretrained checkpoints are downloaded separately by each upstream
script and must be reviewed for license and provenance before use. Export a
compatible TorchScript checkpoint and pass it through
`TorchScriptActiveSpeakerScorer` in `gaeclip.vision_adapters`; the adapter
keeps checkpoint inference on CPU or CUDA and returns one audio-visual score
per face window. The detector output remains the versioned `FaceObservation`
contract, so replacing MediaPipe with YOLO does not change the timeline code.

Untuk memasang dependency yang dipin pada local profile:

```bash
python -m pip install -e ".[dev]"
```

## Documentation

- [Goals](goals.md)
- [Architecture](architecture.md)
- [Roadmap](roadmap.md)
- [Pipeline](docs/pipeline.md)
- [API contract](docs/api-contract.md)
- [Configuration reference](docs/configuration.md)
- [Initial stack ADR](docs/decisions/0001-initial-stack.md)
- [Problem resolution pack](problem/README.md)
- [44-finding solution matrix](problem/SOLUTION-MATRIX.md)
- [Final audit](FINAL-AUDIT.md)

## Frontend design system

Frontend design will use the installed Material 3 skill:

```text
.agents/skills/material-3/
```

(`skills-lock.json` records it as `skills/material-3/SKILL.md` from
`hamen/material-3-skill`; the project checkout path above is canonical.)
The skill references Material 3 color, typography, shape, component, navigation,
responsive layout, and dynamic theming guidance. It is installed as a project
skill so the future Next.js UI follows one consistent design system.

## Local reference

`yt-dlp-reference/` is a shallow, local-only clone of the upstream yt-dlp
repository used for implementation reference. It is intentionally excluded from
version control by `.gitignore`; runtime dependencies must still be pinned and
reviewed independently.
