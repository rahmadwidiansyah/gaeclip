# 4. Local Security dan Abuse Controls

## Local-only decision

User memilih tidak memakai authentication karena aplikasi dijalankan di laptop
sendiri dan tidak public. Keputusan ini diterima **hanya** dengan controls berikut:

- Uvicorn/Next.js/Redis bind `127.0.0.1`, bukan wildcard;
- Compose tidak publish port ke LAN secara default;
- firewall/laptop user menjadi boundary tambahan, bukan satu-satunya control;
- limits, queue admission, disk quota, timeout, dan cleanup tetap wajib;
- README harus memiliki warning ketika user mengubah bind/port/tunnel.

Jika user menjalankan `--host 0.0.0.0`, port-forward, ngrok, Tailscale share,
reverse proxy, atau server, startup harus menolak local mode atau meminta
`PUBLIC_MODE=true` plus authentication/rate limit configuration.

## URL validation

Terima hanya `https://www.youtube.com/...`, `https://youtube.com/...`,
`https://youtu.be/...`, dan host resmi yang memang diperlukan. Reject `file:`,
`ftp:`, `data:`, localhost, loopback, RFC1918/link-local IPv4/IPv6, credentials
di URL, unsupported redirects, dan arbitrary extractor URLs.

Normalisasi URL, hapus tracking parameters yang aman, lalu validasi ulang setelah
redirect. Jalankan yt-dlp tanpa `shell=True`, dengan output path yang dibuat server.

## Filesystem safety

Job ID memakai random server-generated identifier. Semua paths dibuat dari
`STORAGE_ROOT/jobs/{job_id}` dan diverifikasi `resolve().is_relative_to(root)`.
Filenames allowlist-only; input title tidak pernah menjadi filename. Reject
symlink/hardlink surprises, gunakan temp files mode 0600, dan atomic rename.

## Secrets and logs

`.env` local mode harus mode 0600 dan tidak pernah di-commit. API key hanya dibaca
worker/provider client, tidak dikirim ke frontend. Redact headers, query keys,
environment values, dan provider payload secrets pada log/error.

## Dependency supply chain

Pin Python/npm versions dan lockfiles, pin Docker base image digest ketika image
distabilkan, simpan model checksum/license/source, dan jalankan vulnerability scan.
Clone `yt-dlp-reference/` hanya untuk informasi lokal dan di-ignore oleh Git;
runtime memakai dependency release yang dipilih, bukan import dari clone tanpa
review.

## Content privacy

Artifact dan transcript lokal hanya tersedia di job directory. Default retention
24 jam. Source URL dapat mengandung identifiers; redaction policy harus diterapkan
di log. Tidak ada telemetri eksternal selain provider LLM yang dipilih user.
