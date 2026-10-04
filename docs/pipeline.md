# Processing Pipeline

## Stage contract

| Stage | Input | Output |
| --- | --- | --- |
| Ingestion | YouTube URL | video metadata |
| Subtitle | metadata | timestamped transcript |
| Curation | transcript | validated candidate clips |
| Extraction | candidate timestamps | raw short clips |
| Visual analysis | raw clips | faces, speaker scores, timeline |
| Layout | timeline and clip | crop/layout instructions |
| Rendering | media and instructions | final MP4 and metadata |

## Transcript and curation

Transcript lines memiliki `line_id`, `start`, `end`, dan `text`. LLM hanya boleh memilih line ID yang tersedia. Backend memvalidasi JSON, range timestamp, jumlah candidate, dan clip duration sebelum download.

Subtitle source mengikuti `TRANSCRIPT_SOURCE` (`youtube_first` default:
coba subtitle YouTube dulu, gagal baru Whisper; `whisper_only`: selalu
Whisper; `youtube_only`: hanya YouTube, gagal cepat bila kosong).
CLI mengunduh audio dan menjalankan faster-whisper dengan segment timestamps. Candidate kemudian diunduh memakai `--download-sections`, bukan mengunduh video penuh.

## Transcript cache (shared, persistent)

Hasil Whisper yang sukses disimpan sekali dan dipakai ulang semua job
berikutnya untuk video + konfigurasi yang sama — Whisper tidak diulang:

- Kunci sumber: `youtube:<video_id>` (watch/youtu.be/shorts/embed dengan
  query tambahan apa pun memetakan ke satu entri). Kunci konfigurasi hanya
  dari setting yang memengaruhi hasil (`whisper_model`, `language`, VAD);
  setting kurasi/clip tidak masuk hash.
- Penyimpanan: Redis (`gaeclip:transcript:*`: record + index + lock) dan
  artefak `storage/transcripts/<cache_id>.json` (di luar `jobs/`, jadi hapus
  job tidak menghapus cache). Tiap job juga menyimpan `source/transcript.json`
  untuk adopsi job lama yang dibuat sebelum cache ada.
- Sebelum Whisper: lookup → `hit` (pakai langsung, progres "Transkripsi
  dilewati — menggunakan hasil tersimpan") / `miss` (transkripsi + simpan).
  Artefak hilang/rusak/kosong, skema lama, atau durasi sumber berubah
  (>10 dtk) → `invalid`, index dibuang, transkripsi baru jalan. Redis error
  → degradasi ke `miss`, job tidak pernah crash karena cache.
- Job konkuren untuk video+konfigurasi sama dideduplikasi via lock Redis
  (TTL 1 jam anti-deadlock); job kedua menunggu (`wait`, ada progres +
  cek cancel) lalu memakai hasil job pertama.
- Log: `[TRANSCRIPT_CACHE] lookup|hit|miss|invalid|store|reuse`.

## Recreate job

`POST /api/v1/jobs/{id}/recreate` membuat job BARU dari job lama: salin
source + parameter clip, tandai `recreated_from_job_id`, lalu jalankan
pipeline normal — yang otomatis `CACHE HIT` → Whisper dilewati → kurasi
dijalankan ulang penuh (hasil kurasi lama tidak dipakai). Tombol ada di
detail job (dialog + opsi "Transkripsi ulang") dan baris tabel jobs.
Duplikat dilindungi: recreate kedua saat anak pertama masih aktif
mengembalikan anak yang sudah ada. `GET .../recreate-preview` memberi info
ketersediaan transkrip untuk dialog; `GET /api/v1/transcripts/stats`
memberi statistik cache.

### Batas implementasi saat ini (2026-09-14, docs-only)

- Tanpa chunking/hierarchical summarization: full transcript dikirim satu
  prompt (`curation.build_prompt`) dengan guard `MAX_TRANSCRIPT_CHARS`.
- Buffer: `start=max(0,s-buffer)` diterapkan; `end=e+buffer` belum di-clamp
  ke durasi video di `pipeline.py` (target: `min(video.duration,e+buffer)`).
- Hysteresis versi sederhana: `min_switch_seconds=2.0`,
  `min_active_score=0.25`, `sample_step=0.1`; belum ada threshold
  enter/exit terpisah, smoothing, atau max-pan-speed.
- Subtitle: tulis `SRT` + burn-in via filter `subtitles`; belum ada
  `ASS`/word-highlight.

## Visual analysis

Face detection/tracking dan active speaker scoring dilakukan setelah candidate clip tersedia. Analisis full-length video dihindari untuk menghemat CPU/GPU dan storage. Modul `gaeclip.visual` menyediakan kontrak adapter-neutral untuk detector/tracker (bounding box) dan model audio-visual (lip/audio score), sehingga YOLO/TalkNet dapat ditambahkan tanpa mengubah timeline engine.

## Hysteresis

Layout tidak berpindah hanya karena interupsi singkat. `build_speaker_timeline` menyimpan target aktif dan kandidat target secara deterministik; kandidat normal harus bertahan ~2 detik sebelum menggantikan target, sedangkan onset bicara yang kuat dan terkonfirmasi (rise + margin lebar) memakai fast path ~0.5 detik. `make_crop_instructions` mengubah timeline menjadi crop portrait dengan safe margin.

## Rendering

Renderer menyediakan FFmpeg filter graph untuk static center crop, subtitle
burn-in, dynamic crop per segment, dan single-crop 9:16 yang memuat dua wajah
berdekatan bila geometrinya feasible (tidak pernah split-screen: pasangan
berjauhan di-fallback ke single speaker terkuat). `generate --observations
observations.json` mengaktifkan jalur visual tersebut; tanpa observations
pipeline tetap memakai static crop.
Semua path dan timestamp berasal dari validated job data.
