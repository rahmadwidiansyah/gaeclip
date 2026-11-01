# Meta OAuth — Facebook Page + Instagram (GAEClip)

Flow: **Instagram API with Facebook Login + Facebook Pages API**, both via
plain **Facebook Login** (`https://www.facebook.com/{v}/dialog/oauth`).
NOT Business Login for Instagram (`instagram_business_*`, `graph.instagram.com`).
Mencampur keduanya dalam satu authorize URL = `Invalid Scopes`.

## Scope source of truth (`social/publishers/meta.py`)

- `META_FACEBOOK_PAGE_SCOPES`:
  `pages_show_list, pages_read_engagement, pages_manage_posts`
  (`publish_video` sengaja TIDAK diminta: itu permission Live Video API
  untuk live-stream, bukan untuk upload Reels/video biasa via Resumable
  Upload API yang cukup dengan `pages_manage_posts`. Memintanya menyebabkan
  `Invalid Scopes: publish_video` kecuali app punya Live Video API use case.)
- `META_INSTAGRAM_SCOPES`:
  `instagram_basic, instagram_content_publish, pages_show_list, pages_read_engagement`
- Combined legacy `META_OAUTH.scopes` = union (deprecated, jangan dipakai baru).
- `meta_scopes_for("facebook"|"instagram")` = capability-based, no duplicates.
- Dihapus: `business_management` (tidak ada Graph call menggunakannya),
  unprefixed Business-Login scopes tidak pernah diminta di flow ini.

Dependency: `pages_manage_posts` membutuhkan `pages_read_engagement` +
`pages_show_list` (dikirim eksplisit, bukan implisit).

## Token flow

1. System browser → Facebook Login → loopback `/oauth/callback`
   (`http://localhost:<port>/oauth/callback` untuk Meta, bukan `127.0.0.1`).
2. Backend tukar code + `client_secret` (user's own app credential, encrypted).
3. `GET /me/accounts` → pilih Page → simpan `facebook_page_id` + `page_token`.
4. Instagram: `GET /{page-id}?fields=instagram_business_account` → simpan
   `instagram_business_account_id` + `page_token` yang sama.
5. Publish memakai **Page token** (`_effective_page_token`), bukan user token.
   Fallback user token hanya untuk akun lama. `meta_app_id` (publik, bukan
   secret) ikut di `account_meta` untuk sesi Resumable Upload
   `POST /{APP_ID}/uploads`. Kalau akun punya >1 Page, pilih via
   `PUT /api/v1/social/accounts/{id} {facebook_page_id}` (UI dropdown).

## Meta App Dashboard checklist

- App ID + App Secret → Settings → Social (secret tidak pernah ke browser).
- App Mode: Development (admin/developer/tester + test Page + test IG Professional).
- Products: Facebook Login (Client OAuth Login ON, Web OAuth Login ON).
- Instagram product hanya jika memakai IG API via Facebook Login.
- Valid OAuth Redirect URIs: localhost tidak perlu whitelist (dev-mode);
  production butuh URI publik + App Review.
- Permissions: minta minimum di atas; production `pages_*`/`instagram_*`
  butuh Advanced Access / App Review. Dev login sukses ≠ production ready.
- Graph version: `v21.0` terpusat di `GRAPH_VERSION`.

## Invalid Scopes debugging

Backend log SAFE (tanpa secret):
`[META_OAUTH] provider=facebook redirect=http://localhost:… scopes=… graph=…`
Bandingkan dengan error Meta. Penyebab umum:
- meminta `business_management` tanpa Business Manager,
- meminta `publish_video` tanpa Live Video API use case (sudah dihapus dari default),
- mencampur `instagram_basic` dengan `instagram_business_*`,
- app belum mengaktifkan Facebook Login product,
- App Type `Consumer` tidak bisa meminta `pages_*` (harus `Business`/`None`),
- Use Case `Manage everything on your Page` belum ditambahkan ke app,
- Graph version/app type tidak mendukung scope tersebut.

## Capability

- `Facebook: can_publish_page` = Page ditemukan.
- `Instagram: can_publish_reels` = IG Professional linked.
- `Meta connected` tidak berarti keduanya ready; UI tampil per-platform.
