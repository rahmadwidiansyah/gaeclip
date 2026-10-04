# 5. Runtime, Docker, dan Operasional

## Compose profiles

Gunakan profile yang jujur:

- `core`: Redis saja untuk development awal;
- `app`: API + worker + Redis setelah Dockerfiles tersedia;
- `frontend`: Next.js setelah frontend runnable;
- `gpu`: worker image/runtime CUDA optional.

Jangan memakai `docker compose up` seolah semua service siap bila Dockerfile belum
ada. Setiap image memiliki non-root user, pinned base image, healthcheck,
graceful signal handling, dan explicit command.

## Network and ports

Default:

```yaml
ports:
  - "127.0.0.1:8000:8000"
  - "127.0.0.1:3000:3000"
  - "127.0.0.1:6379:6379"
```

Redis tidak perlu diakses browser; pada Compose production-like gunakan internal
network tanpa host publish. API base URL frontend harus cocok dengan binding.

## Readiness

Redis healthcheck memakai `redis-cli ping`. API readiness memeriksa Redis connect,
storage writable, FFmpeg/yt-dlp binary, dan model/provider configuration yang
diperlukan untuk profile. `depends_on` memakai `condition: service_healthy` jika
didukung target Compose.

## Redis policy

Redis local adalah broker/result state sementara. Set TTL result, visibility/
acknowledgement Celery, bounded retries, dan memory limit. Jangan simpan video atau
transcript besar di Redis; simpan metadata/artifact di filesystem/object storage.
Pada local-only mode, Redis dapat tanpa public password karena bind localhost, tetapi
tidak boleh dipublish ke LAN.

## Worker resources

Pisahkan queue `cpu`, `gpu`, dan optional `render`. Set task time limit, soft time
limit, worker concurrency default 1, memory-aware admission, temporary directory
quota, dan subprocess timeout. Satu laptop tidak boleh menerima job baru jika disk
free space atau memory berada di bawah threshold.

GPU profile membutuhkan CUDA-compatible host, NVIDIA container runtime, image
matching driver, model compute type, dan CPU fallback. Startup harus memberi error
jelas jika GPU diminta tetapi unavailable; jangan diam-diam mengubah workload berat
ke CPU tanpa policy.

## Storage and retention

MVP memakai `storage/jobs/{job_id}` single-host. Tambahkan storage adapter interface
(`put`, `open`, `delete`, `exists`, `checksum`) agar object storage dapat mengganti
filesystem. Cleanup scheduler menghapus expired jobs dengan lock, menghitung disk
usage, dan menyimpan cleanup error metrics. Retention: intermediate 6 jam, final
artifact 24 jam default, configurable.

## Retry and failure matrix

| Failure | Retry | Action |
| --- | --- | --- |
| invalid URL/schema | no | fail immediately |
| source unavailable/403 | no | fail with source error |
| network timeout | yes, 3 | exponential backoff + jitter |
| Gemini 429/5xx | yes, 3 | backoff, then deterministic fallback |
| model OOM | no immediate | reduce profile or fail actionable |
| FFmpeg invalid input | no | preserve stderr redacted, fail |
| worker crash | yes | Celery requeue/checkpoint |
| disk full | no immediate | cleanup, then fail |

Clean intermediate files before retry only when safe. Never duplicate external
side effects without idempotency marker.

## Recovery

Job metadata/checkpoints are written atomically. Redis loss may lose queued state
in local MVP; documented behavior is to mark orphaned directories during startup
and offer local cleanup/replay. Final artifacts are not considered backed up.
Production profile requires durable metadata, object storage, backup, and restore
drill before public launch.
