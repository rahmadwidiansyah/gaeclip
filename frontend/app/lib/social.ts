export interface SocialCapabilities {
  direct_publish: boolean;
  draft_upload: boolean;
  scheduling: boolean;
  thumbnail: boolean;
  title: boolean;
  description: boolean;
  caption: boolean;
  hashtags: boolean;
  privacy: boolean;
  status_polling: boolean;
  multiple_accounts: boolean;
  approval_note: string;
  supports_upload_progress?: boolean;
  supports_processing_status?: boolean;
  supports_remote_status_polling?: boolean;
  supports_provider_url?: boolean;
  supports_cancel?: boolean;
  supports_publish_verification?: boolean;
}

export interface SocialAccount {
  id: string;
  provider: string;
  external_account_id: string;
  display_name: string;
  status: "connected" | "needs_reconnect" | "disconnected";
  scopes: string[];
  expires_at: number | null;
  created_at: number;
  updated_at: number;
  meta: Record<string, unknown>;
}

export interface SocialPublishJob {
  id: string;
  clip_id: string;
  job_id: string;
  provider: string;
  social_account_id: string;
  status: string;
  title: string;
  caption: string;
  hashtags: string[];
  privacy: string;
  scheduled_at: number | null;
  started_at: number | null;
  completed_at: number | null;
  attempts: number;
  error_code: string;
  error_message: string;
  remote_media_id: string;
  remote_url: string;
  provider_url?: string;
  created_at: number;
  updated_at: number;
  // Lifecycle detail: upload vs processing are DISTINCT. progress is a REAL
  // 0..1 fraction during UPLOADING only; null = indeterminate (no fakes).
  phase?: string;
  progress?: number | null;
  status_message?: string;
  upload_started_at?: number | null;
  upload_completed_at?: number | null;
  processing_started_at?: number | null;
  published_at?: number | null;
  failed_at?: number | null;
  last_checked_at?: number | null;
  check_count?: number;
  attempt_history?: {
    id: string;
    attempt_number: number;
    status: string;
    error_code: string;
    error_message: string;
  }[];
}

export interface ClipPublishSummary {
  total: number;
  completed: number;
  processing: number;
  failed: number;
  cancelled?: number;
  scheduled?: number;
  active?: number;
  global_status: string;
}

export interface PublishInput {
  clip_id: string;
  job_id: string;
  provider: string;
  account_id: string;
  title: string;
  caption: string;
  hashtags: string[];
  privacy: string;
  scheduled_at: number | null;
}

function withApiToken(init?: RequestInit): RequestInit {
  if (typeof window === "undefined") return init ?? {};
  const token = localStorage.getItem("gaeclip_api_token");
  if (!token) return init ?? {};
  const headers = new Headers(init?.headers);
  if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

async function req(base: string, path: string, init?: RequestInit, fallback?: string) {
  const res = await fetch(`${base}${path}`, withApiToken(init));
  if (!res.ok) {
    let message = fallback ?? "Operasi social gagal";
    let code: string | undefined;
    try {
      const payload = await res.json();
      if (payload?.detail?.message) {
        message = payload.detail.message;
        code = payload.detail.code;
      }
    } catch {
      /* keep fallback */
    }
    const err = new Error(message) as Error & { status: number; code?: string };
    err.status = res.status;
    err.code = code;
    throw err;
  }
  return res.json();
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const listSocialProviders = (base: string) =>
  req(base, "/api/v1/social/providers", undefined, "Gagal memuat provider social") as Promise<{
    providers: Record<string, SocialCapabilities>;
  }>;

export const listSocialAccounts = (base: string, provider = "") =>
  req(
    base,
    `/api/v1/social/accounts${provider ? `?provider=${encodeURIComponent(provider)}` : ""}`,
    undefined,
    "Gagal memuat akun social",
  ) as Promise<{ accounts: SocialAccount[] }>;

export const connectSocialAccount = (base: string, provider: string, redirectUri = "") =>
  req(base, "/api/v1/social/accounts/connect", json({ provider, redirect_uri: redirectUri }), "Gagal memulai login") as Promise<{
    session_id: string;
    authorization_url?: string;
  }>;

export const completeOauthCallback = (base: string, body: { state: string; code?: string; error?: string }) =>
  req(base, "/api/v1/social/oauth/callback", json(body), "Gagal menyelesaikan login") as Promise<{
    status: string;
    account_id?: string;
    error?: string;
  }>;

export const pollOauthSession = (base: string, sessionId: string) =>
  req(base, `/api/v1/social/oauth/session/${encodeURIComponent(sessionId)}`, undefined, "Gagal cek status login") as Promise<{
    status: string;
    provider: string;
    authorization_url?: string;
    account_id?: string;
    error?: string;
  }>;

export const reconnectSocialAccount = (base: string, accountId: string, redirectUri = "") =>
  req(base, `/api/v1/social/accounts/${encodeURIComponent(accountId)}/reconnect`, json({ redirect_uri: redirectUri }), "Gagal reconnect") as Promise<{
    session_id: string;
    authorization_url?: string;
  }>;

export const disconnectSocialAccount = (base: string, accountId: string) =>
  req(
    base,
    `/api/v1/social/accounts/${encodeURIComponent(accountId)}`,
    { method: "DELETE" },
    "Gagal disconnect akun",
  ) as Promise<{ disconnected: string }>;

export const updateSocialAccountPage = (base: string, accountId: string, facebookPageId: string) =>
  req(
    base,
    `/api/v1/social/accounts/${encodeURIComponent(accountId)}`,
    json({ facebook_page_id: facebookPageId }),
    "Gagal ganti Page",
  ) as Promise<SocialAccount>;

export const createPublish = (base: string, input: PublishInput) =>
  req(base, "/api/v1/social/publish", json(input), "Gagal membuat publish job") as Promise<SocialPublishJob>;

export const listPublishJobs = (base: string, clipId = "") =>
  req(
    base,
    `/api/v1/social/publish${clipId ? `?clip_id=${encodeURIComponent(clipId)}` : ""}`,
    undefined,
    "Gagal memuat riwayat publish",
  ) as Promise<{ jobs: SocialPublishJob[]; summary?: ClipPublishSummary }>;

export const getPublishJob = (base: string, id: string) =>
  req(base, `/api/v1/social/publish/${encodeURIComponent(id)}`, undefined, "Gagal memuat publish job") as Promise<SocialPublishJob>;

export const retryPublishJob = (base: string, id: string) =>
  req(base, `/api/v1/social/publish/${encodeURIComponent(id)}/retry`, json({}), "Gagal retry") as Promise<SocialPublishJob>;

export const cancelPublishJob = (base: string, id: string) =>
  req(base, `/api/v1/social/publish/${encodeURIComponent(id)}/cancel`, json({}), "Gagal cancel") as Promise<SocialPublishJob>;

export const refreshPublishJob = (base: string, id: string) =>
  req(base, `/api/v1/social/publish/${encodeURIComponent(id)}/refresh`, json({}), "Gagal refresh status") as Promise<SocialPublishJob>;

/* ── Pure helpers (unit-tested, no network) ── */

const STATUS_LABEL: Record<string, string> = {
  connected: "Connected",
  needs_reconnect: "Needs Reconnect",
  disconnected: "Not Connected",
  pending: "Queued",
  queued: "Queued",
  preparing: "Preparing",
  authorizing: "Authorizing",
  uploading: "Uploading",
  uploaded: "Uploaded",
  processing: "Processing",
  reconciling: "Checking status",
  published: "Published",
  draft: "Draft",
  scheduled: "Scheduled",
  failed: "Failed",
  cancelled: "Cancelled",
  // Global (multi-platform) states.
  publishing: "Publishing",
  completed: "Completed",
  partial_success: "Partial success",
  ready: "Not published",
};

const STATUS_TONE: Record<string, string> = {
  connected: "ok",
  published: "ok",
  completed: "ok",
  uploaded: "ok",
  needs_reconnect: "warn",
  scheduled: "warn",
  reconciling: "warn",
  partial_success: "warn",
  uploading: "info",
  preparing: "info",
  processing: "info",
  publishing: "info",
  pending: "info",
  queued: "info",
  authorizing: "info",
  failed: "err",
  draft: "muted",
  cancelled: "muted",
  disconnected: "muted",
};

export function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

export function statusTone(status: string): string {
  return STATUS_TONE[status] ?? "muted";
}

const TERMINAL_PUBLISH = new Set(["published", "draft", "failed", "cancelled"]);

export function isTerminalPublishStatus(status: string): boolean {
  return TERMINAL_PUBLISH.has(status);
}

/** Active = worker/polling may still advance (upload vs processing distinct). */
export function isActivePublishStatus(status: string): boolean {
  return ["pending", "queued", "preparing", "authorizing", "uploading",
    "uploaded", "processing", "reconciling", "scheduled"].includes(status);
}

/** Default caption = clip title + hashtags (user-editable before publish). */
export function initialCaption(title: string, hashtags: string[]): string {
  const tags = (hashtags ?? [])
    .map((t) => t.trim().replace(/^\s*#/, "").replace(/\s+/g, ""))
    .filter(Boolean)
    .slice(0, 30);
  const head = (title ?? "").trim();
  if (!tags.length) return head;
  return `${head}\n${tags.map((t) => `#${t}`).join(" ")}`.trim();
}

/** Overall "x/4 platforms published" summary for multi-platform results. */
export function publishSummary(jobs: { status: string }[]): string {
  const done = jobs.filter((j) => j.status === "published" || j.status === "draft").length;
  return `${done}/${jobs.length} platforms published`;
}

/** Honest global counts (never averaged percentages). */
export function summarizeJobs(jobs: { status: string }[]): ClipPublishSummary {
  const total = jobs.length;
  const completed = jobs.filter((j) => j.status === "published" || j.status === "draft").length;
  const processing = jobs.filter((j) => j.status === "processing" || j.status === "uploaded").length;
  const failed = jobs.filter((j) => j.status === "failed").length;
  const cancelled = jobs.filter((j) => j.status === "cancelled").length;
  const scheduled = jobs.filter((j) => j.status === "scheduled").length;
  const active = jobs.filter((j) => isActivePublishStatus(j.status)).length;
  return { total, completed, processing, failed, cancelled, scheduled, active, global_status: aggregatePublishStatus(jobs) };
}

export type ClipPublishState =
  | "ready"
  | "active"
  | "publishing"
  | "processing"
  | "queued"
  | "scheduled"
  | "published"
  | "completed"
  | "partial_success"
  | "draft"
  | "failed"
  | "cancelled";

/** Aggregate per-clip publish state (mirrors backend summarize_jobs).
 * One platform failing never marks the others failed: mixed
 * completed+failed with no active reads as partial_success.
 * "active" is kept as a legacy alias of "publishing". */
export function aggregatePublishStatus(jobs: { status: string }[]): ClipPublishState {
  if (jobs.length === 0) return "ready";
  const statuses = jobs.map((j) => j.status);
  const completed = statuses.filter((s) => s === "published" || s === "draft").length;
  const processing = statuses.filter((s) => s === "processing" || s === "uploaded").length;
  const failed = statuses.filter((s) => s === "failed").length;
  const cancelled = statuses.filter((s) => s === "cancelled").length;
  const scheduled = statuses.filter((s) => s === "scheduled").length;
  const activeUpload = statuses.filter((s) =>
    ["pending", "queued", "preparing", "authorizing", "uploading", "reconciling"].includes(s)).length;
  if (cancelled === statuses.length) return "cancelled";
  if (activeUpload > 0) return "publishing";
  if (processing > 0 && processing + scheduled === statuses.length) return "processing";
  if (scheduled === statuses.length) return "scheduled";
  if (completed === statuses.length) return "completed";
  if (failed === statuses.length) return "failed";
  if (completed > 0 && activeUpload + processing === 0) return "partial_success";
  if (activeUpload + processing > 0) return "publishing";
  if (statuses.some((s) => s === "published")) return "published";
  if (statuses.some((s) => s === "draft")) return "draft";
  if (statuses.some((s) => s === "failed")) return "failed";
  if (statuses.some((s) => s === "scheduled")) return "scheduled";
  return "ready";
}

/** Human phase line per platform job. Never returns bare "Progress". */
export function phaseMessage(job: SocialPublishJob): string {
  if (job.status_message) return job.status_message;
  switch (job.status) {
    case "pending":
    case "queued":
      return "Queued · waiting for worker";
    case "preparing":
      return "Preparing upload…";
    case "authorizing":
      return "Authorizing…";
    case "uploading": {
      const p = typeof job.progress === "number" ? Math.round(job.progress * 100) : null;
      return p === null ? "Uploading…" : `Uploading · ${p}%`;
    }
    case "uploaded":
      return "Upload complete · waiting for processing";
    case "processing": {
      const names: Record<string, string> = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram", facebook: "Facebook" };
      const label = names[job.provider] ?? job.provider;
      return `${label} is processing your video`;
    }
    case "reconciling":
      return "Checking upload status…";
    case "published":
      return "Published";
    case "draft":
      return "Saved as draft";
    case "scheduled":
      return "Scheduled";
    case "failed":
      return job.error_message || "Publish failed";
    case "cancelled":
      return "Cancelled";
    default:
      return statusLabel(job.status);
  }
}

/** True only when the provider gave a REAL 0..1 uploading fraction. */
export function determinateProgress(job: SocialPublishJob): number | null {
  if (job.status !== "uploading") return null;
  if (typeof job.progress !== "number" || !Number.isFinite(job.progress)) return null;
  if (job.progress <= 0 || job.progress >= 1) return null;
  return Math.round(job.progress * 100);
}

/** Human error category for failure semantics (never raw exception). */
export function failureKind(errorCode: string): string {
  const code = (errorCode || "").toLowerCase();
  if (code.includes("auth") || code.includes("reconnect") || code.includes("permission")) return "auth";
  if (code.includes("rate") || code.includes("quota") || code.includes("transient")) return "retryable";
  if (code.includes("processing")) return "processing";
  if (code.includes("media") || code.includes("invalid")) return "invalid";
  return "provider";
}

export interface MetadataPackage {
  title: string;
  description: string;
  hashtags: string[];
  source: "gemini" | "deterministic_fallback";
  provider: string | null;
  model: string | null;
  platforms: string[];
  cached?: boolean;
}

export interface MetadataGenerateInput {
  clip_id: string;
  job_id?: string;
  platforms?: string[];
  fields?: ("title" | "description" | "hashtags")[];
  title_override?: string | null;
  description_override?: string | null;
  hashtags_override?: string[] | null;
}

export function generateMetadata(base: string, input: MetadataGenerateInput, opts?: { signal?: AbortSignal }) {
  return req(base, "/api/v1/social/metadata/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      clip_id: input.clip_id,
      job_id: input.job_id ?? "",
      platforms: input.platforms ?? [],
      fields: input.fields ?? ["title", "description", "hashtags"],
      title_override: input.title_override ?? null,
      description_override: input.description_override ?? null,
      hashtags_override: input.hashtags_override ?? null,
    }),
    signal: opts?.signal,
  }, "Gagal generate metadata") as Promise<MetadataPackage>;
}

/** Normalize one hashtag to #word form (backend does the same). */
export function normalizeHashtag(raw: string): string {
  const cleaned = raw
    .replace(/^#+/, "")
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}_]/gu, "");
  return cleaned;
}

/** Parse free-form hashtag input into #word tokens, deduped, max 8. */
export function parseHashtagsInput(value: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const part of value.split(/[\s,]+/)) {
    const tag = normalizeHashtag(part);
    if (!tag || seen.has(tag)) continue;
    seen.add(tag);
    out.push(`#${tag}`);
    if (out.length >= 8) break;
  }
  return out;
}

/** Render hashtag tokens for the text input. */
export function formatHashtagsInput(tags: string[]): string {
  return tags.join(" ");
}

export type StoredClipMeta = {
  description?: string | null;
  hashtags?: unknown;
  metadata_source?: string | null;
} | null | undefined;

/** Initial Publish form values from stored clip metadata (no LLM call).
 *
 * Used once when the dialog opens: prefill description/hashtags saved
 * during clip creation, surface their origin, keep everything editable.
 * Legacy clips (no stored metadata) yield blanks + null source, i.e. the
 * previous manual-generate behavior.
 */
export function initialPublishValues(stored: StoredClipMeta): {
  caption: string;
  hashtags: string;
  aiSource: "gemini" | "deterministic_fallback" | null;
} {
  const caption = typeof stored?.description === "string" ? stored.description.trim() : "";
  const rawTags = Array.isArray(stored?.hashtags) ? stored.hashtags : [];
  const bare = rawTags
    .filter((t): t is string => typeof t === "string")
    .map((t) => t.replace(/^#+/, ""));
  const hashtags = formatHashtagsInput(parseHashtagsInput(bare.join(" ")));
  const src = typeof stored?.metadata_source === "string" ? stored.metadata_source : null;
  const aiSource =
    src === "llm" ? "gemini" : src === "deterministic_fallback" ? "deterministic_fallback" : null;
  return { caption, hashtags, aiSource };
}

/** Timezone options: full IANA list when available, curated fallback. */
export function timeZoneList(): string[] {
  try {
    const fn = (Intl as unknown as { supportedValuesOf?: (x: string) => string[] })
      .supportedValuesOf;
    if (typeof fn === "function") {
      const list = fn.call(Intl, "timeZone");
      if (Array.isArray(list) && list.length > 0) {
        return list.includes("UTC") ? list : ["UTC", ...list];
      }
    }
  } catch {
    /* fall through */
  }
  return ["UTC", "Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura",
    "Asia/Singapore", "Asia/Kuala Lumpur", "Europe/London",
    "America/New_York", "America/Los_Angeles"];
}

/** Guess the browser's IANA timezone for schedule defaults. */
export function localTimeZone(fallback = "Asia/Jakarta"): string {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz) return tz;
  } catch {
    /* ignore */
  }
  return fallback;
}

/** Interpret wall-clock date+time in `tz` as epoch seconds. */
export function zonedLocalToEpoch(dateStr: string, timeStr: string, tz: string): number | null {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dateStr);
  const timeMatch = /^(\d{2}):(\d{2})$/.exec(timeStr);
  if (!dateMatch || !timeMatch) return null;
  const guess = new Date(Date.UTC(
    Number(dateMatch[1]), Number(dateMatch[2]) - 1, Number(dateMatch[3]),
    Number(timeMatch[1]), Number(timeMatch[2]), 0,
  ));
  if (!Number.isFinite(guess.getTime())) return null;
  let offsetMinutes = 0;
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: tz, hour12: false, year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(guess);
    const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
    const asUTC = Date.UTC(
      Number(get("year")), Number(get("month")) - 1, Number(get("day")),
      Number(get("hour")) % 24, Number(get("minute")), Number(get("second")));
    if (!Number.isFinite(asUTC)) return null;
    offsetMinutes = Math.round((asUTC - guess.getTime()) / 60000);
  } catch {
    return null;
  }
  return Math.floor(guess.getTime() / 1000) - offsetMinutes * 60;
}
