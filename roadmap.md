# Roadmap

## Release A — Local CLI and text-to-cut MVP

**Status: selesai sebagai baseline lokal.**

- Setup Python project, pinned dependencies, downloader, FFmpeg wrapper, and
  versioned transcript schema.
- Ambil YouTube subtitle dengan fallback faster-whisper.
- Normalize cue dan stable line IDs.
- Curate candidate timestamps melalui LLM structured output dengan deterministic
  fallback.
- Render static-center 9:16 clip dengan subtitle dasar.
- Sediakan command yang dapat menjalankan URL sampai MP4.

**Out of scope:** API, Celery, Redis, frontend, active speaker, dynamic crop,
dan public deployment.

## Release B — Local API and queue

- Bungkus pipeline Release A menjadi Celery task.
- Tambahkan FastAPI endpoint canonical `/api/v1/jobs`.
- Implementasikan lifecycle `accepted`, `queued`, `running`, `retrying`,
  `completed`, `partial`, `failed`, `cancelled`, dan `expired`.
- Tambahkan progress, idempotency, cancellation, cleanup, healthchecks,
  resource limits, dan job-scoped storage.

## Release C — Visual AI

**Status: baseline selesai (timeline + dynamic crop + detector adapters);
tersisa kalibrasi dan benchmark akurasi.**

- Face detector/tracker baseline tersedia: `MediaPipeFaceDetector` dan
  `YOLOFaceDetector` di `src/gaeclip/vision_adapters.py` + checkpoint
  `models/yolo26n-face.pt`.
- Active speaker scoring tersedia via `TorchScriptActiveSpeakerScorer`
  (batas TorchScript `(visual_tensor, audio_tensor)` -> skor), skor
  `(lip*0.55+audio*0.45)*confidence`.
- Deterministic speaker timeline (`min_switch_seconds=2.0`,
  `min_active_score=0.25`) + dynamic crop safe-margin + split-screen
  tersedia di `src/gaeclip/visual.py`; CLI `analyze-visual` dan
  `benchmark-vision` tersedia.
- Tersisa: kalibrasi threshold terpisah enter/exit, smoothing/max-pan-speed,
  evaluasi presisi/recall pada dataset berlisensi (bukan frame sintetik).

## Release D — Web UI and polish

**Status: baseline selesai (submit + polling + download + token M3);
tersisa polish.**

- Next.js submit form, polling 2 detik, progress/error state, dan download
  artifact tersedia di `frontend/app/page.tsx`.
- Token Material 3 tersedia di `frontend/app/globals.css` (termasuk
  `tertiary-container`/`error-container` light+dark).
- Tersisa: tombol cancel, preview `<video>`, pemakaian endpoint `/results`,
  accessibility checks, observability/retention UI.

## Release gates

Setiap release harus memiliki happy-path test, failure-path test, reproducible
local setup, versioned input/output schema, dan dokumentasi. Release berikutnya
tidak dimulai hanya karena source code ada; gate harus lulus.
