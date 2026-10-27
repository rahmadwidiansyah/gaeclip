# Plan: Broker OAuth terpusat + landing page (`gaeclip.sanding.online`)

Status: **PLAN — belum dieksekusi.** Dokumen ini satu-satunya sumber kebenaran
rencana; ubah dokumen ini dulu sebelum mengubah kode.

Keputusan yang sudah dikunci:

- **1a — single-operator.** Host hanya melayani instalasi milik sendiri
  (N mesin, 1 operator). Tanpa login user, tanpa multi-tenant.
- **2a — token disimpan di instalasi.** Host hanya perantara tukar
  `code → token` (transit Redis ≤ 10 menit, sekali ambil langsung hapus).
  Host tidak menyimpan token user.
- **Key tetap `SOCIAL_*`.** Tanpa rename/migrasi DB. Label UI boleh dirapikan.

```
[Desktop / Docker user] --(1. start + broker key)--> [gaeclip.sanding.online]
        |                                                        |
        +--(2. browser login medsos via URL resmi)--+             |
        |                                          v             |
        |                              provider redirect ke host/callback
        |                                                        |
        +--(3. poll hasil + broker key)--> token dikirim sekali --+
        |                                    (sesi dihapus setelah diambil)
   simpan di SecretStore lokal, publish tetap lokal seperti sekarang
```

Model lama (kredensial per mesin, `zero credentials` di binary) tetap didukung
sebagai fallback bila broker kosong/tak-terjangkau. Lihat
`docs/social-publishing.md:9-12` untuk model lama.

---

## P1. Auth antar-mesin + setting key baru

Tujuan: instalasi bisa menunjuk ke host + membuktikan identitasnya dengan
pre-shared key. Tanpa ini siapa pun bisa minta login atas nama host.

### P1.1 `src/gaeclip/app_settings.py`

- Tambah ke `EMPTY_OK_KEYS` (`:37-40`): `"SOCIAL_BROKER_URL"`.
  Alasan: tipe `string` kosong ditolak `_parse` (`:276`) kecuali ada di set ini.
  URL kosong = broker nonaktif (valid).
- Sisip 3 `SettingDef` sebelum `)` penutup di `:226-229`
  (kategori `"Social Publishing"`, `apply_note="Berlaku untuk koneksi baru."`).
  Contoh persis yang ditiru:
  - string → `SOCIAL_YOUTUBE_CLIENT_ID` (`:204-206`)
  - secret → `SOCIAL_YOUTUBE_CLIENT_SECRET` (`:207-209`)
  - boolean → `AI_FREE_TIER_ONLY` (`:87-89`, default `"True"`)
- Definisi yang ditambah:

```python
SettingDef("SOCIAL_BROKER_URL", "string", "Social Publishing", "Broker OAuth URL",
           "SOCIAL_BROKER_URL", "Cth: https://gaeclip.sanding.online. Kosong = pakai kredensial sendiri.",
           "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
SettingDef("SOCIAL_BROKER_ENABLED", "boolean", "Social Publishing", "Pakai Broker OAuth",
           "SOCIAL_BROKER_ENABLED", "ON = Connect medsos via broker host. OFF = kredensial sendiri.",
           "False", None, None, None, None, False, "Berlaku untuk koneksi baru."),
SettingDef("SOCIAL_BROKER_KEY", "secret", "Social Publishing", "Broker API Key",
           "SOCIAL_BROKER_KEY", "Pre-shared key dengan host. Tidak pernah ditampilkan kembali.",
           "", None, None, None, None, False, "Berlaku untuk koneksi baru."),
```

- Tidak perlu route/API baru: `GET/PATCH/DELETE /api/v1/settings`
  (`src/gaeclip/api.py:281-326`) generik; `as_dict()` otomatis memask secret;
  `migrate_from_env()` otomatis memungut env baru.

### P1.2 Host secret (bukan DB)

- Host baca `BROKER_API_KEY` dari **env proses** (`os.getenv` di modul broker),
  bukan dari `AppSettings`/DB. Alasan: ini secret host, bukan setting instalasi.
- Perbandingan wajib constant-time (`hmac.compare_digest`).

### P1.3 Verifikasi P1

- `GET /api/v1/settings` menampilkan 3 key baru; secret termask
  (`value=None`, cuma `configured`).
- `PATCH` set + `DELETE` reset untuk ketiganya.
- `pytest tests/test_app_settings.py -q` hijau.

---

## P2. Broker API di host (modul baru, greenfield)

Tidak ada kode `broker/session_ref/relay` di `src/gaeclip/social/*` hari ini
(yang ada cuma Celery `broker=REDIS_URL`) — jadi tidak ada tabrakan nama.

### P2.1 File baru `src/gaeclip/social/broker.py`

- `BrokerSessionStore(client)`: tiru pola `SocialAccountStore`
  (`accounts.py:157-204`). Key `gaeclip:social:broker:<session_ref>`,
  tulis via `client.set(key, json.dumps(...), ex=600)` (preseden `ex/nx` di
  `jobs.py:109`; didukung Redis + SQLite `sqlite_client.py:98-119`).
  Isi JSON: `{session_id, session_ref, provider, state, code_verifier,
  redirect_uri, authorization_url, status, created_at}`.
  **Tidak menyimpan** `client_secret`/token di sesi — secret dibaca dari
  Settings host saat dibutuhkan, token transit hanya ditulis saat callback
  (lihat P2.2) dengan TTL 10 menit.
- `session_ref`: `broker_<uuid hex 12>` (opaque lintas backend; beda dari
  `session_id=oauth_<hex>` yang lokal per backend — UI tidak boleh menukarnya).

### P2.2 Route (daftar di `src/gaeclip/api.py:1373-1375` sebelah `register_social_routes`)

1. `POST /api/v1/social/oauth/broker/start {provider, key}`
   → validasi key → `publisher_for(provider)` (nama provider valid) →
   buat sesi kv → `build_authorization_url(host_client_id, config,
   redirect_uri=https://gaeclip.sanding.online/oauth/callback)` →
   `{session_id, session_ref, authorization_url}`.
   Kredensial yang dipakai = `SOCIAL_*` milik **host** (via
   `_oauth_client_config`, `service.py:41-56`).
2. `GET /oauth/callback` (publik, dipanggil provider via browser):
   halaman sukses/gagal sederhana. Ambil `{code|error, state}`,
   cocokkan `state` ke sesi kv → tukar `code→token` pakai secret host
   (`exchange_code`, `oauth.py:242-257`) → tulis hasil transit
   `{status, credentials|error}` TTL 600 dtk. `state` tak dikenal →
   halaman error generik (tanpa detail).
3. `GET /api/v1/social/oauth/broker/session/{session_ref}` (+key) →
   `{status, provider, authorization_url?, error?}` untuk polling instalasi
   (tiru bentuk `oauth_flow.poll`, `:248-263`, tanpa secret).
4. `POST /api/v1/social/oauth/broker/result {session_ref, key}` →
   kembalikan kredensial **sekali ambil lalu `delete`** (semantik GETDEL);
   unknown/expired → `AuthError("sesi tidak dikenal/kedaluwarsa")`.

### P2.3 Anti-open-redirector

- `_valid_broker_redirect()` baru: hanya string persis
  `https://gaeclip.sanding.online/oauth/callback`.
- **Jangan** melonggarkan `_valid_frontend_redirect`
  (`oauth_flow.py:45-56`) yang loopback-only.

### P2.4 Konsol provider (manual, di luar kode — checklist saat eksekusi)

- YouTube: OAuth client tipe **Web** + Authorized redirect URI di atas +
  client secret (flow web butuh secret; flow desktop lama tidak).
- TikTok: daftarkan redirect URI yang sama di Login Kit.
- Meta: Valid OAuth Redirect URIs + Client OAuth Login ON.
- Layar consent akan bermerek milikmu — konsekuensi desain 1a.

### P2.5 Verifikasi P2

- Test mocked: key salah → 401; `state` asing → error; ambil result 2x →
  kedua gagal; sesi kedaluwarsa; respons/log tidak mengandung secret/token.
- `curl` start → callback (simulasi provider) → result → Redis bersih
  (tidak ada key `gaeclip:social:broker:*` tersisa).

---

## P3. Mode relay di instalasi (tanpa merusak 2 mode lama)

Mode lama yang tidak boleh berubah perilaku bila broker nonaktif:
frontend-callback (`oauth_flow.py:75-102`) dan loopback-listener (`:103-110`).

### P3.1 `oauth_flow.py`

- Sisip cabang relay sesudah blok `if redirect_uri:` (`:75-102`), sebelum
  `thread=...` (`:103`):

```python
def start(..., redirect_uri: str = "", broker_redirect_uri: str = "",
          mode: str = "", session_ref: str = "") -> dict[str, Any]:
    ...
    if <broker diminta>:
        return start_broker(client, provider, client_id, session_id,
                            broker_redirect_uri, ...)
```

- `start_broker`: panggil host `broker/start` (bawa key dari Settings),
  simpan `{session_ref, broker_url}` di sesi lokal (bukan secret),
  kembalikan `{session_id, session_ref?, authorization_url}`.
- `complete_by_session_ref()` — kembaran `complete_by_state` (`:151-220`):
  lookup by `session_ref`, CAS `pending→completing` + `_await_winner`
  yang sama (`:223-240`), panggil host `broker/result`, lalu
  `normalize_credentials` + `publisher.account_info()` +
  `SecretStore.put()` **lokal** (reuse `_finish` `:337-392` dengan refactor
  parameter agar exchange bisa dilewat).
- Token exchange lokal dilewat total di mode broker; `client_secret`
  instalasi tidak pernah dipakai/dikirim.

### P3.2 `service.py`

- `connect_start(provider, redirect_uri="", *, broker_redirect_uri="",
  mode="")` + 3 metode baru: `connect_start_broker`,
  `complete_broker_callback`, `connect_poll_broker`. Validasi provider via
  `publisher_for` dulu (pola `api.py:163`).
- Refresh token YouTube-web butuh secret → ikut via broker
  (endpoint `broker/refresh` tambahan) atau minta Reconnect. Putuskan saat
  implementasi; rekomen via broker.

### P3.3 `social/api.py` + frontend

- Body baru `BrokerConnectBody/BrokerCallbackBody` di samping
  `ConnectBody:28`; 3 route broker di area `:179-182`; reuse `_service()`
  + `_social_error()`.
- `frontend/app/lib/social.ts`: `connectBrokerAccount`,
  `completeBrokerCallback`, `pollBrokerSession` (cermin 3 fungsi lama
  `:142-162`).
- `frontend/app/oauth/callback/page.tsx:21-50`: cabang `session_ref`
  (dari query) vs `state`; bentuk respons sama
  `{status,account_id,error}` sehingga `setState/setMessage` dipakai ulang.
- `social-accounts.tsx:82-88` `goConnect`: bila respons tanpa
  `authorization_url` (kasus async), polling
  `pollOauthSession/pollBrokerSession` sampai URL muncul (hari ini langsung
  throw `"Backend tidak memberi URL login."` di `:87`).
- `session_id` (kunci poll lokal) vs `session_ref` (kunci relay antar
  backend) tidak boleh tertukar di UI.

### P3.4 Verifikasi P3

- Broker nonaktif → semua test OAuth lama hijau (tidak ada perubahan perilaku).
- Broker aktif: Connect → URL resmi → callback → token masuk SecretStore
  lokal → akun `connected`; matikan broker di tengah jalan → error jelas,
  fallback kredensial lokal bisa dipilih manual.

---

## P4. UI broker + badge

- `settings/page.tsx` grup ke-4 `Broker` di Developer Configuration
  (`:1195-1255`, pola array group `youtube/tiktok/meta`): `idKeys`
  `[SOCIAL_BROKER_URL (+ENABLED bila checkbox)]`, `secretKeys`
  `[SOCIAL_BROKER_KEY]`; tombol **Uji Koneksi** di dalam Collapsible
  (pola `onTest` `:769-790`, helper `testBroker` di `lib/api.ts` sebelah
  `testGemini`, state pesan ala `testMsg :563`).
- **Wajib**: cabang `boolean` di `renderField` (`:809-833`, hari ini boolean
  jatuh ke input angka) → `<input type="checkbox">` (contoh
  `ai-models.tsx:267`); konversi `"True"/"False"` ↔ boolean untuk
  `patchAppSettings`.
- `social-account-card.tsx:53-59`: prop `viaBroker?` → chip `via broker`
  di `social-card-top` sebelah chip status. `social-accounts.tsx:177`
  isi dari `meta.via_broker` (backend tandai di `_finish` saat via broker;
  perlu 1 baris + tipe `SocialAccount.meta` di `lib/social.ts:22-33`
  yang hari ini tanpa field broker).

---

## P5. Checklist konsol provider

Termasuk di P2.4 di atas (sengaja tidak jadi paket sendiri).

---

## P6. Infra publik host

Keadaan hari ini: `infra/` kosong; tanpa proxy/TLS (`grep caddy|nginx`
nol hasil); compose publish loopback-only; `NEXT_PUBLIC_API_BASE_URL`
di-bake saat `npm run build` tanpa `ARG` (`Dockerfile:5-10`); `PUBLIC_MODE`
menolak bind loopback (`config.py:139-140`).

### P6.1 Baru

- `infra/Caddyfile`: TLS otomatis `gaeclip.sanding.online`;
  `reverse_proxy /api* /health* → app:8000`, `/app* + / → app:3000`;
  `encode gzip` + header keamanan.
- `docker-compose.public.yml`: `include: [docker-compose.prod.yml]` +
  service `caddy:2-alpine` (port 80/443, volume Caddyfile read-only +
  `caddy-data/caddy-config`), override `app.ports: []` (tutup `:8000`
  dari publik), network bersama.
- `infra/README.md`: runbook `DOMAIN + ACME_EMAIL + up -d`.

### P6.2 Ubah

- `.env.example`: `DOMAIN=`, `ACME_EMAIL=`, contoh
  `FRONTEND_ORIGINS=https://DOMAIN`,
  `NEXT_PUBLIC_API_BASE_URL=https://DOMAIN` (same-origin),
  catatan `API_HOST=0.0.0.0` + `PUBLIC_MODE=true`.
- `Dockerfile:5-10`: `ARG NEXT_PUBLIC_API_BASE_URL=...` +
  `ENV ...` sebelum build; `docker-compose.*` teruskan build-arg;
  `.github/workflows/docker.yml` teruskan `build-args` (hari ini tidak ada).
  Tanpa ini browser publik tetap memanggil `127.0.0.1:8000`.
- `SETTINGS_SECRET` wajib di host via env (bukan file, bukan git).
- CORS `api.py:139-153` via env (tanpa ubah kode bila env benar).

### P6.3 Verifikasi P6

- `curl https://gaeclip.sanding.online/health/config` OK + TLS valid;
  `:8000` tidak terekspos; `GET /docs` tidak publik;
  `docker compose -f docker-compose.public.yml up -d` dari nol di VPS bersih.

---

## P7. Landing page + pindah app ke `/app`

Keadaan hari ini: `/` = dashboard fungsional (`app/page.tsx:72`),
`Shell` membungkus semua route (`layout.tsx:22-28`),
`apiBase()` bake + override localStorage (`lib/api.ts:14-21`).

### P7.1 Pindah

- Baru: `frontend/app/page.tsx` landing statis (tanpa `Shell`, tanpa `apiBase`).
- Pindah: dashboard `app/page.tsx:72-346` → `app/app/page.tsx`
  (atau grup `(app)` + layout pemilik `Shell`); cabut `Shell` global dari
  `layout.tsx`; `jobs/ clips/ settings/ oauth/callback/` → bawah `/app/*`;
  `shell.tsx:9-14` href → `/app*`; `router.push(/jobs/…)` →
  `/app/jobs/…`; `callback/page.tsx:58` link → `/app/settings`.
- Sinkronkan validator backend `_valid_frontend_redirect`
  (`oauth_flow.py:45-56`) + dok redirect Meta karena origin callback jadi
  `/app/oauth/callback`. `src-tauri` hardcode 127.0.0.1 tidak tersentuh.

### P7.2 Verifikasi P7

- `/` tanpa sidebar, `/app` dashboard normal, 1 Connect via broker end-to-end
  dari rute baru, `npm test` (38 suite) hijau.

---

## P8. Dok + test + rilis

- Dok: `docs/social-publishing.md` (model broker), `docs/configuration.md`,
  `.env.example`, README publik, `infra/README.md`.
- Test (mocked, tanpa network): P1 CRUD+masking; P2 key-salah/state-asing/
  ambil-2x/sesi-kedaluwarsa/tanpa-bocor-secret; P3 fallback lokal;
  P6 `PUBLIC_MODE`+CORS; P7 route `/` vs `/app` + callback.
  Lalu `pytest` + `npm test` penuh.
- Rilis: image GHCR publik di-build dengan
  `NEXT_PUBLIC_API_BASE_URL=https://gaeclip.sanding.online`; instalasi user
  pull biasa + isi 3 setting broker.

---

## Risiko & batas scope (dicatat, tidak dikerjakan Fase 1)

1. Kuota gabungan 1 App ID (YouTube ±10.000 unit/hari/project) — untuk
   pribadi aman; monitor bila perlu.
2. Token transit ≤10 mnt di Redis host (keputusan 2a) — one-time-pickup +
   TLS + key antar-mesin.
3. Publish/worker tetap lokal; host down → Connect baru gagal, token lama
   tetap jalan.
4. **Di luar scope:** multi-tenant (login user, vault per-user, rate limit
   per user, App Review massal, billing). Ditolak masuk Fase 1.

## Urutan eksekusi

**P1 → P2 → P3 → P4 → P6 → P7 → P8**, dengan checklist konsol provider
(P2.4) paralel kapan saja. Perkiraan bobot: P1 kecil, P2–P3 inti,
P6–P7 sedang.
