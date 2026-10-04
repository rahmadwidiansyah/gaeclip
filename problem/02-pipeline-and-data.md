# 2. Pipeline dan Data Correctness

## Versioned data model

Gunakan Pydantic/dataclass model dengan `schema_version`:

```json
{
  "schema_version": 1,
  "line_id": "l000123",
  "start": 12.34,
  "end": 15.67,
  "text": "Contoh kalimat.",
  "words": [
    {"text": "Contoh", "start": 12.34, "end": 12.90, "confidence": 0.91}
  ],
  "language": "id",
  "source": "youtube_auto|faster_whisper",
  "confidence": 0.91
}
```

Semua waktu adalah `float` seconds dalam satu time base UTC-independent. `start`
inklusif, `end` eksklusif, finite, `0 <= start < end`. Word timestamps optional;
line timestamps wajib.

`CandidateClip` wajib memiliki `candidate_id`, `start_line_id`, `end_line_id`,
`start`, `end`, `title`, `reason`, `score` dalam `[0,1]`, dan `schema_version`.
`LayoutSegment` wajib memiliki `start`, `end`, `layout`, `target_face_id?`,
`confidence`, dan transition reason. Job/artifact schema menyimpan checksum dan
pipeline version.

## Subtitle normalization and stable line IDs

1. Parse SRT/VTT/YouTube JSON ke intermediate cue.
2. Drop cue kosong dan trim Unicode whitespace.
3. Clamp timestamps, reject non-finite/negative values, dan sort `(start,end,index)`.
4. Normalize line breaks dan Unicode normalization NFC.
5. Deduplicate cue yang text dan interval-nya identik/overlap penuh.
6. Merge cue yang merupakan line wrap jika gap <= 250 ms dan total duration masih
   <= 7 detik; jangan merge jika ada gap speech yang nyata.
7. Assign ID setelah normalisasi dengan format `l000001`, monotonik dan immutable.
8. Simpan mapping raw cue -> normalized line untuk audit.
9. Emit transcript JSON plus SRT/ASS dari model yang sama, bukan parse ulang.

## Curation

Jangan selalu mengirim transcript tiga jam sebagai satu prompt. Pipeline:

1. Hitung char/token estimate.
2. Jika di bawah limit, kirim sekali.
3. Jika melebihi limit, chunk berdasarkan window waktu dengan overlap 1-2 line.
4. Minta shortlist per chunk dengan schema strict.
5. Deduplicate/merge shortlist berdasarkan interval overlap.
6. Jalankan final ranking pada ringkasan/shortlist yang kecil.

Prompt memiliki version, output JSON schema, language, clip limits, dan instruksi
bahwa transcript adalah data tidak tepercaya; abaikan instruksi yang berada di
dalam transcript. Cache berdasarkan transcript hash, prompt version, model,
language, dan curation config. Simpan usage/cost metadata, bukan API key.

## LLM validation and fallback

Strict validation menolak unknown line IDs, invalid ranges, NaN/Infinity, score di
luar range, duplicate candidate, overlap di atas configured threshold, title
terlalu panjang, dan clip di luar limits. Tidak ada arbitrary repair yang dapat
mengubah maksud model.

Jika curation gagal atau menghasilkan nol candidate:

1. retry hanya untuk provider transient error dengan exponential backoff;
2. gunakan deterministic fallback berbasis transcript windows;
3. tandai `curation_source=fallback`;
4. jika fallback juga gagal, job `failed` dengan actionable error.

## Smart extraction

Gunakan `yt-dlp` hanya dengan argument list, explicit format policy, output
directory job-scoped, dan filename template server-generated. Partial extraction
adalah optimization, bukan correctness guarantee. Jika format/protocol tidak
mendukung range, download format yang dipilih lalu trim dengan FFmpeg.

Buffer:

```text
start = max(0, candidate.start - buffer)
end   = min(video.duration, candidate.end + buffer)
```

Merge extraction ranges yang overlap untuk menghindari download ganda, tetapi
render tiap candidate tetap memakai timestamp relatif yang benar. Selalu lakukan
post-trim dengan FFmpeg dan verifikasi duration.

## Whisper fallback

Subtitle dianggap unusable bila kosong, parse error, coverage terlalu rendah,
timestamp invalid, atau language mismatch sesuai policy. faster-whisper config:
model name, device, compute type, beam size, language, VAD, model cache path,
dan download timeout harus eksplisit. Model dimuat sekali per worker dan startup
failure terlihat sebagai health/readiness failure.

## Active speaker baseline

Pilih baseline bertahap:

1. Release A/B: tidak ada CV; static crop.
2. Release C baseline: face detector/tracker yang dipilih setelah benchmark pada
   sample podcast berlisensi.
3. Audio evidence: VAD/diarization optional dan visual score tidak boleh menjadi
   satu-satunya bukti.
4. Model adapter memiliki `detect_faces`, `track_faces`, `score_active_speaker`,
   model version, device, dan confidence output.

Dokumentasikan weight source, checksum, license, preprocessing, FPS sampling,
minimum face size, occlusion behavior, dan CPU/GPU mode sebelum model diterima.

## Hysteresis state machine

State menyimpan `current_target`, `pending_target`, `pending_since`,
`last_switch_at`, dan `layout`. Rules:

- unknown/silence mempertahankan target sampai `max_hold_seconds`, lalu fallback
  ke `wide`/center;
- target baru harus confidence di atas `ENTER_THRESHOLD` terus-menerus selama
  `SWITCH_CONFIRM_SECONDS=2.0`;
- target current dipertahankan dengan threshold exit lebih rendah;
- switch tidak boleh lebih sering daripada `MIN_HOLD_SECONDS`;
- tie memakai target dengan confidence stabil lebih lama, lalu face position;
- overlap speech dapat memilih `split` hanya jika dua confidence memenuhi threshold
  selama `SPLIT_CONFIRM_SECONDS`;
- interruption di bawah 2 detik tidak mengganti target.

State machine murni dan deterministic; tidak boleh memanggil model atau clock
global. Semua edge case memiliki table-driven tests.

## Crop and subtitle rendering

Crop planner mengubah face box ke 9:16 dengan safe margin, clamp ke frame,
exponential/savitzky smoothing, dan max pan speed. Jika face hilang, tahan posisi
terakhir sebentar lalu fallback center/wide. Jika target terlalu kecil atau lebih
dari satu face dominan, gunakan wide/split policy.

Render contract menetapkan resolution, FPS, codec, bitrate/CRF, audio codec,
subtitle style, safe area, font package, Unicode normalization, dan libass
availability. Generate ASS dari word timestamps; jangan membangun filter graph
dengan shell interpolation. Simpan filter script dan ffprobe result per job.
