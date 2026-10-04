# Audit Rencana Gaeclip

## Ringkasan

Rencana saat ini sudah menggambarkan visi produk dan alur besar yang masuk akal: transcript/LLM diproses lebih dahulu, lalu computer vision hanya dijalankan pada candidate clips. Namun dokumen tersebut belum siap dijadikan spesifikasi implementasi. Masih ada celah pada definisi MVP, kontrak data, keamanan, reliability, deployment, biaya, validasi kualitas, dan kesiapan Docker.

Masalah paling kritis adalah:

1. `docker-compose.yml` belum dapat dijalankan karena `api/`, `worker/`, dan `frontend/` belum memiliki Dockerfile atau aplikasi.
2. API tanpa authentication, quota, atau rate limit dapat disalahgunakan untuk download dan rendering yang mahal.
3. Storage dan download artifact belum memiliki authorization, retention, cleanup, atau batas ukuran.
4. Kontrak pipeline belum mendefinisikan schema yang dapat diimplementasikan dan diuji.
5. Ketergantungan model active-speaker belum dipilih, belum ada dataset/metric, dan aturan hysteresis 2 detik terlalu sederhana untuk semua kondisi audio-video.
6. Fase dan target MVP bertentangan: dokumen menyebut CLI MVP, tetapi skeleton sudah mengunci API, worker, frontend, dan Compose sebelum core pipeline terbukti.

Severity yang digunakan:

- **Critical**: dapat membuat sistem tidak dapat dijalankan atau membuka penyalahgunaan serius.
- **High**: dapat menyebabkan kegagalan utama, biaya besar, atau data/artifact tidak aman.
- **Medium**: menghambat correctness, operasional, atau maintainability.
- **Low**: ketidakjelasan atau perbaikan kualitas yang sebaiknya ditangani sebelum production.

---

## 1. Masalah requirement dan scope

### 1.1 MVP belum memiliki satu definisi yang konsisten — **High**

`planawal.txt` mendefinisikan Phase 1 sebagai CLI tanpa face detection. `goals.md` menyebut asynchronous Celery/Redis dan API sebagai MVP goals. `README.md` menyebut CLI/API MVP, sedangkan `docker-compose.yml` sudah mendefinisikan frontend.

**Dampak:** implementasi dapat melebar ke empat service sebelum pipeline sederhana terbukti, sehingga debugging dan pengukuran progress menjadi sulit.

**Perbaikan:** tetapkan release pertama secara eksplisit, misalnya:

- Release A: CLI lokal, subtitle/transcription, LLM curation, static crop, dan output MP4.
- Release B: API + Celery/Redis.
- Release C: active speaker dan dynamic layout.
- Release D: frontend.

Setiap release harus memiliki in-scope, out-of-scope, acceptance criteria, dan dependency yang jelas.

### 1.2 Tidak ada product constraints yang terukur — **High**

Belum ada batas maksimum durasi video, jumlah clip, durasi clip, resolusi, ukuran file, concurrent jobs, waktu proses, CPU/RAM/VRAM, atau budget per job.

**Dampak:** satu request dapat menghabiskan resource worker atau biaya Gemini secara tidak terkendali.

**Perbaikan:** definisikan default dan hard limit, misalnya `max_source_duration`, `max_clips`, `min/max_clip_duration`, `max_upload/artifact_size`, timeout setiap stage, concurrency worker, dan budget/token limit LLM.

### 1.3 Sumber dan hak penggunaan konten belum dibahas — **High**

Sistem mengunduh dan mengolah video YouTube. Tidak ada kebijakan tentang copyright, Terms of Service, private/age-restricted video, geo-restriction, content ownership, takedown, atau retention.

**Dampak:** risiko legal dan operasional, khususnya bila aplikasi dipublikasikan atau hasil disimpan lama.

**Perbaikan:** tambahkan policy bahwa user hanya memproses konten yang berhak mereka gunakan, dukung error untuk video yang tidak dapat diakses, dan tentukan retention/takedown policy.

### 1.4 Acceptance criteria terlalu umum — **Medium**

“MP4 dapat diputar” tidak cukup untuk memverifikasi kualitas. Belum ada target aspect ratio, frame rate, audio codec, subtitle readability, timestamp accuracy, crop safety, atau jumlah candidate yang valid.

**Perbaikan:** tetapkan technical acceptance tests untuk media metadata dan visual sample, bukan hanya file existence.

---

## 2. Masalah pipeline dan data correctness

### 2.1 Transcript schema belum benar-benar didefinisikan — **High**

Dokumen hanya menyebut `line_id`, `start`, `end`, dan `text`. Belum ada schema formal untuk word timestamps, confidence, language, source, speaker, ordering, time base, atau format serialization.

**Dampak:** output `yt-dlp`, SRT/VTT, dan faster-whisper dapat dipetakan secara berbeda dan menghasilkan timestamp yang tidak konsisten.

**Perbaikan:** tetapkan versioned JSON schema/dataclass untuk transcript, word, candidate, layout segment, job, dan artifact.

### 2.2 “Line ID stabil” belum memiliki algoritma — **High**

Subtitle YouTube dapat memiliki overlap, line wrapping berbeda, cue kosong, timestamp kurang presisi, dan format yang berbeda antar bahasa. Penggabungan subtitle menjadi line ID belum dijelaskan.

**Dampak:** LLM dapat memilih ID yang benar secara sintaks tetapi timestamp clip salah.

**Perbaikan:** definisikan normalisasi cue, ordering, deduplication, merging window, rounding policy, dan validasi bahwa setiap ID memetakan ke interval asli.

### 2.3 LLM diminta membaca transcript utuh tanpa chunking atau budget — **High**

Rencana mengirim transcript utuh ke LLM. Video tiga jam dapat menghasilkan prompt besar; context window, token cost, latency, rate limit, dan kualitas seleksi belum dikontrol.

**Perbaikan:** tetapkan chunking/hierarchical summarization, token budget, maximum transcript size, language handling, prompt version, model fallback, dan cache berdasarkan transcript hash.

### 2.4 Validasi output LLM belum lengkap — **High**

Baru disebut validasi JSON, line ID, range, jumlah candidate, dan duration. Belum ada schema enforcement, duplicate overlap, sorting, minimum hook quality, invalid score handling, hallucinated title constraints, dan fallback bila output kosong.

**Perbaikan:** gunakan typed schema dengan strict parsing, reject/repair policy terbatas, deterministic fallback candidate, dan simpan raw response untuk debugging tanpa membocorkan secret.

### 2.5 Smart download dengan `yt-dlp` belum dibuktikan untuk semua format — **High**

Partial range download dari YouTube bergantung pada format, extractor, keyframe, container, network, dan dukungan external downloader. `yt-dlp + ffmpeg` tidak otomatis menjamin byte-range yang efisien atau frame-accurate.

**Dampak:** biaya download mungkin tetap besar, cut dapat meleset, atau hasil tidak memiliki audio/video yang kompatibel.

**Perbaikan:** tentukan strategi format, fallback ketika range download gagal, keyframe tolerance, post-trim dengan FFmpeg, serta test matrix untuk format/resolusi sumber.

### 2.6 Buffer timestamp belum memiliki aturan — **Medium**

Plan menyebut buffer 1-2 detik tetapi tidak menentukan clamp ke `0` dan durasi video, pembulatan, overlap antar candidate, atau akibat buffer terhadap durasi maksimum.

**Perbaikan:** definisikan `start = max(0, start-buffer)`, `end = min(video_duration, end+buffer)`, merge policy, dan batas durasi final.

### 2.7 Fallback transcription belum membedakan failure modes — **Medium**

Tidak jelas kapan subtitle dianggap tidak tersedia, rusak, terlalu pendek, salah bahasa, atau tidak sinkron. Tidak ada aturan model size, compute type, device, download model, atau retry.

**Perbaikan:** definisikan subtitle quality checks, model/configuration matrix, model cache, startup validation, timeout, dan error code.

### 2.8 Active speaker detection belum merupakan keputusan teknis — **Critical**

Dokumen menyebut YOLOv8-face, MediaPipe, SyncNet, dan TalkNet sebagai alternatif. Tidak ada pilihan final, versi model, weight source, license, preprocessing, frame sampling, face tracking identity, audio alignment, atau inference device.

**Dampak:** komponen inti tidak bisa diimplementasikan reproducibly dan hasil antar environment dapat berbeda.

**Perbaikan:** pilih baseline model untuk MVP, dokumentasikan weight/license, input-output contract, model download strategy, dan siapkan adapter untuk penggantian model.

### 2.9 Lip movement bukan bukti tunggal active speaker — **High**

Gerakan bibir dapat tidak terlihat karena angle, blur, occlusion, microphone delay, dubbing, laughter, overlap speech, atau orang berbicara di luar frame. “Confidence” belum didefinisikan dan belum ada audio voice activity/speaker diarization.

**Perbaikan:** gabungkan visual score dengan audio VAD/speaker evidence, smoothing temporal, no-face state, low-confidence state, dan evaluasi pada dataset percakapan nyata.

### 2.10 Hysteresis 2 detik terlalu kaku dan belum mendefinisikan edge cases — **High**

Belum dijelaskan initial target, silence, tie, simultaneous speakers, speaker kembali sebelum 2 detik, confidence threshold, end-of-clip, scene cut, dan apakah 2 detik dihitung continuous atau akumulatif.

**Perbaikan:** tulis state machine formal dengan threshold terpisah untuk enter/exit, minimum hold time, tie-breaker, unknown layout, dan test vectors timeline.

### 2.11 Dynamic crop belum memiliki geometry rules — **High**

Tidak ada aturan memilih crop 9:16 dari bounding box, smoothing posisi, padding, batas frame, perubahan aspect ratio, camera shot wide, atau ketika lebih dari satu wajah masuk area crop.

**Dampak:** wajah dapat terpotong, crop dapat bergetar, dan output tidak usable meskipun speaker detection benar.

**Perbaikan:** definisikan crop planner, safe margins, interpolation, max pan speed, fallback center crop, dan golden media tests.

### 2.12 Subtitle rendering belum spesifik — **Medium**

Pysubs2, ASS, font, fallback font, Unicode/RTL, word highlight, line breaking, safe area, dan subtitle timing belum ditetapkan. Filter `subtitles` memerlukan font/libass yang benar di image.

**Perbaikan:** tetapkan style schema, bundled fonts dengan license yang sesuai, line wrapping policy, dan test untuk Bahasa Indonesia serta karakter non-ASCII.

---

## 3. Masalah API, job lifecycle, dan state

### 3.1 API contract masih draft dan tidak implementable penuh — **High**

Status code untuk not-ready, not-found, invalid input, rate limit, dan internal error belum ditentukan. Pagination, download auth, cancellation, retry, idempotency, dan versioning belum ditentukan.

**Perbaikan:** bekukan OpenAPI contract sebelum API implementation, termasuk status codes, enums, schema version, correlation ID, dan compatibility policy.

### 3.2 Endpoint naming tidak konsisten dengan plan awal — **Medium**

`docs/api-contract.md` memakai `/api/v1/jobs`, sedangkan `planawal.txt` menyebut `/api/generate-clips`. Belum diputuskan apakah frontend memakai proxy atau API base URL langsung.

**Perbaikan:** pilih satu canonical endpoint dan update seluruh dokumen.

### 3.3 Job status hanya empat state — **Medium**

`queued`, `processing`, `completed`, dan `failed` tidak cukup untuk retry, cancellation, expiration, partial success, atau per-stage progress.

**Perbaikan:** definisikan lifecycle seperti `queued`, `running`, `retrying`, `completed`, `partial`, `failed`, `cancelled`, `expired`, termasuk terminal-state rules.

### 3.4 Progress percentage belum punya definisi — **Medium**

Contoh `progress: 35` tidak menjelaskan bobot setiap stage atau cara menangani candidate parallelism.

**Perbaikan:** gunakan stage enum plus monotonic progress policy; dokumentasikan bobot atau jangan menjanjikan persentase presisi.

### 3.5 Idempotency belum terhubung ke API — **High**

Architecture menyebut task harus idempotent, tetapi request tidak memiliki idempotency key, deduplication policy, atau aturan duplicate submission.

**Perbaikan:** tambahkan `Idempotency-Key`, request fingerprint, job ownership, dan aturan apakah duplicate URL boleh membuat job baru.

### 3.6 Tidak ada cancellation dan cleanup API — **Medium**

Job video dapat memakan resource besar, tetapi user tidak dapat membatalkan job atau menghapus hasil.

**Perbaikan:** tambahkan cancel/delete endpoint dan cooperative cancellation di setiap stage.

### 3.7 Tidak ada observability contract — **Medium**

Progress dan error context disebut, tetapi tidak ada structured log fields, correlation ID, metrics, tracing, atau admin inspection.

**Perbaikan:** tetapkan `job_id`, `task_id`, stage, duration, error code, retry count, model version, dan source URL redaction sebagai standard fields.

---

## 4. Masalah keamanan dan abuse prevention

### 4.1 API tanpa authentication/rate limit dapat menjadi layanan publik penyedot resource — **Critical**

Rencana sengaja menunda login, tetapi tidak menggantinya dengan API key internal, access token sementara, rate limit, concurrency limit, atau quota.

**Dampak:** siapa pun yang dapat mengakses API dapat memicu download, LLM calls, CPU/GPU inference, dan storage growth.

**Perbaikan:** untuk local-only MVP bind ke localhost; untuk deployment network tambahkan authentication minimal, rate limit, per-client quota, max duration/clips, dan admission control.

### 4.2 URL input belum memiliki validasi keamanan — **High**

`source_url` diteruskan ke yt-dlp, tetapi allowlist host, scheme, redirect policy, private URL protection, dan extractor restrictions belum ditentukan.

**Perbaikan:** izinkan hanya `https`, allowlist YouTube host yang diperlukan, tolak localhost/private IP/unsupported schemes, dan jangan meneruskan input mentah ke shell.

### 4.3 Artifact download belum memiliki authorization — **High**

Contract mengembalikan `download_url`, tetapi tidak menjelaskan apakah URL public, signed, scoped ke user/job, atau hanya internal.

**Perbaikan:** gunakan opaque job IDs, authorization check, expiring signed URL atau authenticated streaming, dan jangan expose filesystem path.

### 4.4 Storage path dan file name belum di-hardening — **High**

Job-scoped storage disebut, tetapi aturan pembuatan job ID, safe filename, symlink, path traversal, disk quota, dan atomic write belum ada.

**Perbaikan:** generate ID server-side, resolve path dan pastikan tetap di storage root, gunakan allowlisted filenames, atomic temp-to-final rename, dan reject symlink.

### 4.5 Secret handling belum lengkap — **Medium**

`GEMINI_API_KEY` ada di environment, tetapi tidak ada `.env` permission policy, secret manager, redaction, atau larangan memasukkan key ke logs.

**Perbaikan:** dokumentasikan secret handling dan pastikan compose/logging tidak menampilkan environment sensitif.

### 4.6 Dependency dan model supply chain belum dikendalikan — **Medium**

Tidak ada pinned image digest, Python dependency lockfile, npm lockfile, model checksum, atau trusted source policy.

**Perbaikan:** pin versions/digests setelah stack dipilih, simpan lockfiles, verifikasi model artifact, dan scan image/dependencies sebelum deployment.

---

## 5. Masalah Docker dan deployment

### 5.1 `docker compose build` pasti gagal pada skeleton saat ini — **Critical**

Compose menunjuk `build: ./api`, `./worker`, dan `./frontend`, tetapi direktori tersebut hanya berisi `.gitkeep`; tidak ada Dockerfile, source, atau dependency manifest.

**Perbaikan:** jangan menyatakan Compose sebagai local setup yang dapat dijalankan sebelum image skeleton tersedia. Pilih salah satu:

- buat Dockerfile minimal dan health endpoint/worker command yang valid; atau
- hapus service yang belum implementable dan dokumentasikan Compose sebagai future scaffold.

### 5.2 API dan frontend tidak mem-publish port — **High**

Hanya Redis yang mem-publish `6379`. Browser tidak dapat mengakses frontend dan user tidak dapat mengakses API dari host.

**Perbaikan:** tambahkan port sesuai aplikasi atau gunakan reverse proxy yang terdokumentasi.

### 5.3 `depends_on` tidak menunggu service ready — **Medium**

`depends_on` hanya mengatur urutan start, bukan health/readiness. API/worker dapat boot sebelum Redis siap.

**Perbaikan:** tambahkan healthcheck Redis dan readiness/retry policy di API/worker.

### 5.4 Redis deployment belum aman atau durable — **High**

Redis dipublish ke host tanpa password/TLS/network restriction, tanpa volume, tanpa maxmemory policy, dan tanpa pemisahan broker/result retention.

**Perbaikan:** local-only binding untuk development, authentication/network isolation, persistence policy sesuai kebutuhan, TTL result, dan resource limits.

### 5.5 GPU configuration belum operasional — **Medium**

Plan menyebut GPU tetapi Compose yang dibuat tidak memiliki device reservation, CUDA base image, runtime compatibility, atau CPU fallback policy.

**Perbaikan:** buat profile CPU/GPU terpisah dengan documented prerequisites dan model compute type.

### 5.6 Shared local storage bukan model deployment yang aman — **High**

Bind mount `./storage` cocok untuk single-host development, tetapi tidak cocok untuk worker horizontal scaling, container replacement, atau multi-node deployment.

**Perbaikan:** nyatakan batas single-node MVP dan buat storage interface; untuk production gunakan object storage dengan lifecycle policy.

### 5.7 Tidak ada resource limits dan process isolation — **High**

Tidak ada CPU, memory, PID, ephemeral storage, timeout, atau concurrency limits di Compose/Celery.

**Perbaikan:** tentukan worker concurrency, task time limits, container limits, temp directory quotas, dan queue separation untuk CPU/GPU jobs.

### 5.8 Tidak ada healthcheck, restart policy, atau graceful shutdown — **Medium**

Kegagalan service tidak memiliki automatic recovery atau readiness signal.

**Perbaikan:** tambahkan health endpoints, Compose healthchecks, restart policy untuk deployment target, dan task acknowledgement/requeue policy.

---

## 6. Masalah dependency, biaya, dan operasional

### 6.1 Klaim provider/model belum divalidasi — **High**

Rencana mengandalkan Gemini 1.5 Flash, GPT-4o-mini, TalkNet/SyncNet, YOLOv8-face, dan faster-whisper tanpa mengunci availability, API version, license, biaya, rate limit, atau maintenance status.

**Perbaikan:** buat compatibility matrix dan pilih satu baseline per komponen sebelum implementation. Hindari menjadikan nama model sebagai abstraction contract.

### 6.2 Cost model tidak ada — **High**

Tidak ada estimasi token, LLM calls/job, download egress, storage growth, model compute, dan retry cost.

**Perbaikan:** tambahkan cost budget per job, token meter, cache policy, retention, dan circuit breaker ketika provider cost/rate limit tercapai.

### 6.3 Retry policy belum aman untuk operasi non-idempotent — **High**

Dokumen menyebut controlled retries tetapi belum menentukan stage mana yang retryable, backoff, max attempts, dan cleanup sebelum retry.

**Perbaikan:** buat error taxonomy (`invalid`, `transient`, `provider`, `resource`, `internal`) dan retry matrix per stage.

### 6.4 Tidak ada retention dan garbage collection — **High**

Intermediate audio, video, subtitle, logs, dan final MP4 dapat menumpuk tanpa expiry.

**Perbaikan:** definisikan TTL per artifact, cleanup task yang aman, quota storage, dan perilaku ketika cleanup gagal.

### 6.5 Tidak ada backup atau recovery policy — **Medium**

Belum jelas artifact mana yang harus recoverable, bagaimana Redis loss ditangani, dan apakah job dapat dilanjutkan atau harus diulang.

**Perbaikan:** tentukan durability target untuk metadata/result, checkpoint stage, dan recovery behavior.

---

## 7. Masalah testing dan quality assurance

### 7.1 Tidak ada test structure atau test tooling — **High**

Release gates menyebut happy/failure-path test, tetapi belum ada test directory, framework, fixture, sample media, atau CI command.

**Perbaikan:** tetapkan pytest untuk unit/integration tests, fixture transcript, fake downloader/LLM, FFmpeg smoke fixture, dan contract tests.

### 7.2 Tidak ada deterministic test untuk hysteresis — **High**

Aturan 2 detik adalah business logic penting, tetapi belum ada input timeline dan expected layout timeline.

**Perbaikan:** buat table-driven tests untuk single speaker, short interruption, sustained switch, overlap, silence, missing face, dan tie.

### 7.3 Tidak ada test untuk malformed/untrusted LLM output — **High**

Prompt atau structured output bukan jaminan provider selalu memberi data valid.

**Perbaikan:** uji missing IDs, negative timestamps, NaN/large scores, duplicate clips, overlapping ranges, invalid JSON, oversized output, dan prompt injection dari transcript.

### 7.4 Tidak ada media regression tests — **Medium**

Perubahan FFmpeg filter dapat mematahkan aspect ratio, audio, subtitle, atau crop tanpa terdeteksi.

**Perbaikan:** gunakan sample videos berlisensi, ffprobe assertions, frame snapshots/golden checks, dan audio stream checks.

### 7.5 Tidak ada performance/load test — **Medium**

Tidak ada validasi throughput, queue latency, memory peak, GPU utilization, atau behavior ketika beberapa job berjalan bersamaan.

**Perbaikan:** definisikan benchmark source durations dan load scenarios sebelum membuka akses multi-user.

---

## 8. Masalah dokumentasi dan konsistensi

### 8.1 README mengklaim local setup tetapi setup belum runnable — **High**

README menyarankan `docker compose up redis`, tetapi tidak menjelaskan bahwa API/worker/frontend belum ada dan tidak menyediakan endpoint yang bisa dipakai.

**Perbaikan:** label sebagai “documentation scaffold” atau lengkapi runnable development setup.

### 8.2 Dokumen tidak mencantumkan version pin dan compatibility — **Medium**

Python version, Node version, FFmpeg version, Redis version, model versions, dan browser/API compatibility belum ditentukan.

**Perbaikan:** tambahkan supported versions dan upgrade policy.

### 8.3 Konfigurasi environment tidak lengkap — **Medium**

`API_BASE_URL` dan `LOG_LEVEL` ada di `.env.example`, tetapi belum dipakai dalam Compose contract. Sebaliknya, port, concurrency, timeouts, limits, model settings, CORS, dan retention tidak tersedia.

**Perbaikan:** buat configuration reference yang memetakan setiap variable ke service, type, default, secret status, dan validation rule.

### 8.4 Storage path tidak konsisten dengan source plan — **Low**

`planawal.txt` menggunakan `./shared_storage`, sedangkan skeleton menggunakan `./storage`. Architecture memilih `storage/jobs/{job_id}`.

**Perbaikan:** pilih `storage/` sebagai canonical path dan hapus istilah lama dari dokumentasi atau jelaskan migration mapping.

### 8.5 Rencana tidak memiliki decision log untuk open questions — **Medium**

ADR awal menerima stack, tetapi keputusan penting masih terbuka: model active speaker, object storage, auth, retention, GPU, source validation, dan API lifecycle.

**Perbaikan:** buat daftar open decisions dengan owner, evidence required, dan decision deadline sebelum komponen terkait diimplementasikan.

---

## Prioritas perbaikan sebelum coding

### P0 — wajib sebelum menjalankan atau membuka sistem

1. Tetapkan Release A/MVP yang benar-benar sempit.
2. Pilih baseline active-speaker approach atau keluarkan dari MVP.
3. Tambahkan batas durasi, clips, file size, concurrency, timeout, dan token budget.
4. Rancang minimal authentication/rate limiting atau batasi deployment ke localhost.
5. Validasi URL YouTube dan amankan artifact download/storage.
6. Putuskan apakah Compose harus runnable sekarang; jika iya, tambahkan Dockerfiles dan aplikasi minimal.

### P1 — wajib sebelum API/worker dianggap production-ready

1. Bekukan versioned schemas untuk transcript, candidate, layout, job, dan artifact.
2. Finalisasi API/OpenAPI contract, lifecycle, idempotency, cancellation, dan error taxonomy.
3. Definisikan retries, cleanup, retention, Redis policy, healthchecks, dan resource limits.
4. Buat unit, integration, contract, deterministic hysteresis, dan media smoke tests.
5. Buat cost model dan provider/model compatibility matrix.

### P2 — wajib sebelum public launch

1. Authentication, authorization, quotas, audit logging, dan signed downloads.
2. Object storage, lifecycle policy, backup/recovery, monitoring, alerting, dan tracing.
3. Copyright/ToS policy, abuse handling, privacy/retention policy, dan deployment hardening.
4. Load testing dan quality benchmark untuk transcription, curation, active speaker, crop, dan subtitle.

## Kesimpulan

Arah produk dan prinsip candidate-first sudah baik, tetapi dokumen saat ini masih berupa concept plan, bukan implementation-ready specification. Langkah paling aman bukan langsung mengimplementasikan semua service, melainkan memperbaiki P0, membuktikan Release A sebagai CLI pipeline terukur, lalu mengunci schema dan contract berdasarkan hasil eksperimen nyata.
