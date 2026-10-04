# 3. API, Job Lifecycle, dan State

## Local API

Canonical endpoints:

```text
POST /api/v1/jobs
GET  /api/v1/jobs/{job_id}
POST /api/v1/jobs/{job_id}/cancel
GET  /api/v1/jobs/{job_id}/results
GET  /api/v1/jobs/{job_id}/clips/{clip_id}
DELETE /api/v1/jobs/{job_id}
GET  /health/live
GET  /health/ready
```

Default server hanya listen pada `127.0.0.1`. Tidak ada endpoint `/api/generate-clips`
legacy; dokumentasi dan frontend memakai canonical path di atas.

## Request and response rules

`POST /jobs` menerima URL, language optional, max clips optional, profile optional,
dan `Idempotency-Key` optional tetapi direkomendasikan. Server melakukan validation
dan mengembalikan `202` hanya setelah job record dibuat dan enqueue berhasil.

Status codes:

- `202`: accepted dan queued;
- `400`: malformed JSON/request;
- `404`: job/clip tidak ditemukan;
- `409`: job belum completed untuk results, atau idempotency conflict;
- `413`: melewati file/duration/size limit;
- `422`: URL/domain/config tidak valid;
- `429`: local admission limit tercapai;
- `500`: unexpected internal error dengan correlation ID;
- `502/503`: provider/dependency failure yang retryable.

Error shape:

```json
{
  "error": {
    "code": "SOURCE_DURATION_LIMIT",
    "message": "Source duration exceeds the configured limit.",
    "retryable": false,
    "correlation_id": "req_01..."
  }
}
```

Message tidak membocorkan command line, filesystem path, secret, atau raw provider
response. Raw details hanya ada di local redacted log.

## Lifecycle

```text
accepted -> queued -> running -> retrying -> running
                             |-> completed
                             |-> partial
                             |-> failed
                             |-> cancelled
                             |-> expired
```

`retrying` dan `partial` harus muncul dalam schema walau Release B mungkin hanya
mengaktifkan subset. Terminal states immutable kecuali admin/local replay membuat
job baru.

`progress` adalah integer monotonic 0-100 yang berbasis stage weights. Client juga
menerima `stage`, `stage_progress`, `updated_at`, dan `eta_seconds?`; ETA optional
dan tidak boleh dianggap akurat.

## Idempotency and duplicate jobs

Generate job ID server-side. Simpan request fingerprint dari normalized URL dan
effective config. Bila `Idempotency-Key` sama, kembalikan job lama jika payload
sama; payload berbeda menghasilkan `409`. Tanpa key, duplicate job diperbolehkan
tetapi local admission limit mencegah overload.

Celery task menerima job ID, bukan URL mentah sebagai identity. Task stage menulis
marker/checkpoint atomically dan aman diulang.

## Cancellation, deletion, and artifacts

Cancel mengubah job ke `cancel_requested`; worker memeriksa flag antar stage dan
menghentikan subprocess dengan timeout lalu membuang intermediate files. Delete
hanya menghapus job-scoped storage setelah task berhenti dan memberi tombstone
metadata minimum untuk mencegah race.

Artifact endpoint melakukan allowlist clip ID, tidak mengembalikan filesystem path,
dan stream hanya file yang telah selesai serta lolos checksum/ffprobe. Pada local
mode, authorization digantikan oleh localhost boundary; jika boundary berubah,
signed URL/auth harus ditambahkan.

## Observability

Setiap request/task memiliki `correlation_id`, `job_id`, `task_id`, `stage`,
`pipeline_version`, `model_version`, `duration_ms`, `retry_count`, `error_code`,
dan redacted source host. Metrics minimum: jobs by status, stage duration, queue
latency, failure count, bytes downloaded, LLM tokens/cost, disk usage, dan worker
memory.
