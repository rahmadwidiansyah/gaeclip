# Social Publishing — platform notes & limitations

> Status jujur per §51/§61: YouTube = implemented + unit-tested with fakes
> (no live credentials in repo); TikTok/Meta = implemented against public
> docs, **NOT integration-tested**. Never claim an untested path works.

## OAuth credential model (reviewed per provider)

The three providers do NOT share one OAuth model. Rule: the distributed
app (installer/Docker image) contains **zero credentials** — everything
below is entered by the operator into Settings and encrypted at rest.

| Provider | OAuth model | Identity field | PKCE | Secret required? |
|---|---|---|---|---|
| YouTube/Google | **Public client** (installed/desktop app type) | `client_id` | ✅ yes (verified) | ❌ no — Desktop clients need none; the secret field stays optional/empty |
| TikTok | **Confidential client** (Login Kit) | `client_key` | ❌ no (unverified — state enforced instead) | ✅ yes (`client_key` + `client_secret` at token endpoint) |
| Meta (IG/FB) | **Confidential client** (Facebook Login) | `client_id` | ❌ no (unverified — state enforced instead) | ✅ yes (app secret mandatory at token exchange) |

Where each credential lives:

- **Windows Desktop:** operator's OWN app credentials in Settings → Social →
  Kredensial OAuth (secret-type = Fernet-encrypted in SQLite). OAuth
  *tokens* additionally go through `SecretStore` (DPAPI-wrapped).
  Loopback `127.0.0.1:<ephemeral>` callback, single-shot, always closed.
- **Docker/Server:** same Settings keys (or env override); tokens via
  `ServerSecretStore` (Fernet from `SETTINGS_SECRET`).
- **Never:** a shared/built-in client secret in source, image, or installer.
  CI and tests assert this (no `client_secret` literals; token payload tests
  pin per-provider field names).

If a provider later mandates a server-side component, connect is refused
with an explicit `ApprovalRequiredError` — never an unofficial workaround.

## YouTube (Data API v3) — implemented + tested (fakes)

- OAuth scopes: `youtube.upload` + `youtube.readonly` (channel label).
  Desktop (installed-app) clients need no client secret; loopback
  `127.0.0.1:<ephemeral>` callback + PKCE.
- Upload: resumable sessions, 8 MiB chunks, progress callbacks, no re-encode
  (final GAEClip MP4 uploaded as-is).
- Metadata: title (≤100), description (≤5000), tags are NOT sent (kept for
  future use), privacy public/unlisted/private.
- Native scheduling: `status.publishAt` (video stays private until then).
- Status polling: `videos.list` processingDetails → PROCESSING/PUBLISHED.
- Quota: uploads cost ~1600 units; default 10.000 units/day per project.

## TikTok (Content Posting API v2) — implemented, NOT tested

- Needs a TikTok developer app with Login Kit + video.upload scope.
- **Direct Post requires audit approval** (`video.publish` scope). Until
  approved, GAEClip uploads as **DRAFT** (UI labels it Draft, never
  Published). `approval_note` is surfaced in Settings.
- Draft vs direct is chosen per account (`allow_direct_post` flips only
  after approval is confirmed).
- OAuth redirect: register loopback in the TikTok portal; if TikTok
  rejects localhost, connect surfaces the provider error (no workaround).

## Instagram (Graph API) — implemented, NOT tested

- **Business/Creator account REQUIRED.** Personal accounts get
  "Instagram account does not meet publishing requirements." (validated
  via `account_type`, never pretended).
- Usually linked through a Facebook Page (portal shows which Page).
- Upload: resumable sessions (no public URL needed on desktop).
- No native scheduling via this API → GAEClip-side wait (works only
  while the app/worker is alive; UI says so).

## Facebook (Graph API) — implemented, NOT tested

- **A Facebook Page is REQUIRED** (user timelines don't take API reels).
  Settings → Social shows a Page picker when the account owns >1 Page
  (default = first Page, switch via PUT /accounts/{id}).
- Upload: Resumable Upload API — `POST /{APP_ID}/uploads` →
  `POST /upload:<SESSION>` chunks (8 MiB awal, mengecil adaptif sampai
  256 KiB, resume dalam sesi via `GET` offset, hormati `backoff` 429) →
  `POST /{page}/videos` multipart (`title + description +
  fbuploader_video_file_chunk=<h dari transfer>`). Kalau host generik
  mentok di offset yang sama (offset-wall), otomatis fallback ke phased
  upload Page (`upload_phase=start/transfer/finish` di `/{page}/videos`;
  finish di-retry 15/30/60s karena 390/1363030 timeout itu transient). Short vertical
  videos are reels-eligible automatically. The old `/{page}/video_uploads`
  edge is dead (Graph 2500) and no longer used.
- No native scheduling → same GAEClip-side wait caveat as Instagram.

## Meta redirect policy (verified — read before debugging login)

Meta's HTTPS enforcement exempts ONLY the literal hostname `localhost`
for apps in **Development mode** — the IP literal `127.0.0.1` is treated
as an insecure non-localhost URL and rejected with *"isn't using a secure
connection"*. Consequences, enforced in code (`redirect_host="localhost"`
on `META_OAUTH`, YouTube/TikTok untouched):

- GAEClip always sends Meta `http://localhost:{port}/oauth/callback`,
  whether the UI runs on `127.0.0.1:3000` or `localhost:3000`, and whether
  the callback lands on the frontend page or the backend listener
  (which binds the same hostname for resolver consistency).
- The Meta app itself must stay in **Development mode** with **Client
  OAuth Login + Web OAuth Login ON**. `localhost` needs NO entry under
  Valid OAuth Redirect URIs.
- NO HTTPS server, NO tunnel, NO public callback — none is required for
  this flow. Do NOT "fix" it with `https://127.0.0.1:...` (no TLS
  listener exists; Meta would still reject the IP literal).

## Secrets & OAuth (all platforms)

- Desktop: DPAPI-protected blobs (Windows user profile), Fernet fallback.
  SQLite holds metadata only (provider, account id, display name,
  token_reference, expiry, status).
- Server: Fernet via `SETTINGS_SECRET` (same as Gemini keys).
- Loopback listener: 127.0.0.1 ephemeral port, single callback, always
  closed. State + PKCE validated. No `0.0.0.0`, no permanent listener,
  no tunnels, no public server.
- Logs/errors never contain codes, access/refresh tokens, or secrets
  (covered by unit tests).

## OAuth callback reachability (important)

The loopback listener runs **inside the backend process**. The browser must
reach that same loopback interface:

- **Windows Desktop (Tauri sidecar):** backend and browser share the machine.
  Defaults (`SOCIAL_OAUTH_HOST=127.0.0.1`, `SOCIAL_OAUTH_PORT=0`) just work.
- **Docker on the same machine as the browser:** container loopback is NOT
  host loopback. Set `SOCIAL_OAUTH_HOST=0.0.0.0` + `SOCIAL_OAUTH_PORT=8765`
  (any free fixed port), and publish it loopback-only:
  `127.0.0.1:8765:8765` on the backend/sidecar container. The host side
  stays loopback-only; only the container binds wide (isolated netns).
  `ERR_CONNECTION_REFUSED` on the callback URL means this mapping is
  missing (or a stale URL from an expired session).
- **Remote server (browser elsewhere):** loopback cannot work. Use an SSH
  tunnel (`ssh -L 8765:localhost:8765 server` + fixed port above) for the
  one-time connect, or run connect from desktop against the server's API
  with a tunnel. Never expose the callback port publicly.

## Setup checklist (per platform portal)

1. YouTube: Google Cloud Console → OAuth client ID, type **Desktop app** →
   paste Client ID into Settings → Social → Kredensial OAuth.
2. TikTok: TikTok for Developers → app with Login Kit → Client Key/Secret →
   same Settings form. Request `video.publish` + audit for Direct Post.
3. Meta: Meta App Dashboard → app with Facebook Login + Instagram/FB
   permissions → App ID/Secret → same Settings form.
4. Settings → Social → Social Accounts → Connect → system browser →
   approve → account appears. Publish from any finished clip.
