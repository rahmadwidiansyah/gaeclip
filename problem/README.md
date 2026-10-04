# Problem Resolution Pack

Dokumen ini adalah rancangan solusi untuk seluruh **44 masalah** yang ditemukan di
`PLAN-AUDIT.md`. Scope operasional yang dipilih adalah **single-user, laptop-local,
non-public**. Karena itu user authentication untuk public multi-tenant tidak
diimplementasikan pada fase ini, tetapi API tetap harus bind ke `127.0.0.1`,
memiliki limits, dan tidak boleh diekspos ke LAN/WAN secara default.

## Dokumen

| File | Isi |
| --- | --- |
| `01-scope-and-product.md` | Scope, release gates, limits, content policy, acceptance criteria |
| `02-pipeline-and-data.md` | Transcript, timestamps, LLM, downloader, Whisper, CV, hysteresis, crop, subtitle |
| `03-api-and-jobs.md` | API contract, lifecycle, idempotency, cancellation, observability |
| `04-local-security.md` | Local-only threat model, URL validation, artifacts, secrets, dependencies |
| `05-runtime-and-deployment.md` | Docker, Redis, GPU, storage, resources, health, retries, retention, recovery |
| `06-testing-and-quality.md` | Test pyramid, fixtures, deterministic CV logic, media and load QA |
| `07-open-decisions.md` | Decision register and implementation order |
| `SOLUTION-MATRIX.md` | Mapping eksplisit problem 1-44 ke solusi dan dokumen |

## Operating profile

### Default local profile

- API bind: `127.0.0.1:8000`
- Frontend bind: `127.0.0.1:3000`
- Redis bind: `127.0.0.1:6379`, bukan `0.0.0.0`
- Satu user, satu laptop, tanpa public authentication
- Default source duration: 2 jam; hard limit: 6 jam
- Default candidate clips: 3; hard limit: 5
- Default clip duration: 15-90 detik
- Satu active generation job pada laptop
- Artifact retention default: 24 jam, configurable

### Important boundary

“Tidak ada auth” hanya berlaku karena service tidak boleh dapat diakses dari luar
laptop. Jika bind address, port forwarding, tunnel, reverse proxy, atau deployment
berubah, local profile tidak lagi valid dan auth/rate limit public wajib diaktifkan.

## Implementation order

1. Bekukan limits, schema, error taxonomy, dan local network binding.
2. Implementasikan Release A: CLI transcript, curation, static crop, subtitle, output.
3. Tambahkan tests dan media fixtures sebelum provider/AI complexity diperluas.
4. Tambahkan API, Redis, Celery, dan job lifecycle.
5. Tambahkan active speaker baseline dan hysteresis dengan offline fixtures.
6. Tambahkan frontend setelah API contract stabil.

Dokumen ini adalah design baseline, bukan bukti bahwa implementation sudah
selesai. Source code, Dockerfiles, schemas, dan tests tetap harus dibuat sesuai
urutan di `07-open-decisions.md`.

> Status implementasi 2026-09-14: baseline CLI/API/worker/visual/UI sudah ada
> di `src/gaeclip/`, `api/Dockerfile`, `worker/Dockerfile`,
> `frontend/app/`, dan `tests/`. Design vs implementasi gaps dicatat jujur di
> `FINAL-AUDIT.md` (Verifikasi Ulang), `roadmap.md`, `docs/pipeline.md`,
> `docs/api-contract.md`, `docs/configuration.md`, dan `docs/visual-models.md`.
> File-file `problem/` di bawah tidak diubah dan tetap menjadi rujukan desain.
