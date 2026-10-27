import type {
  AiModelsResponse,
  AiRefreshResult,
  AiSelectionResponse,
  AppSetting,
  ChannelInfo,
  CreateJobInput,
  Job,
  JobSearchParams,
  JobSearchResult,
  ServerConfig,
} from "./types";

export function defaultApiBase(): string {
  return process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
}

export function apiBase(): string {
  if (typeof window === "undefined") return defaultApiBase();
  return localStorage.getItem("gaeclip_api_base") || defaultApiBase();
}

const API_TOKEN_KEY = "gaeclip_api_token";

export function apiToken(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem(API_TOKEN_KEY) || "";
}

export function saveApiToken(value: string): void {
  if (typeof window === "undefined") return;
  const token = value.trim();
  if (token) localStorage.setItem(API_TOKEN_KEY, token);
  else localStorage.removeItem(API_TOKEN_KEY);
}

function withAuth(init?: RequestInit): RequestInit {
  const token = apiToken();
  if (!token) return init ?? {};
  const headers = new Headers(init?.headers);
  if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function parseError(res: Response, fallback: string): Promise<never> {
  let message = fallback;
  let code: string | undefined;
  try {
    const payload = await res.json();
    const detail = payload?.detail;
    if (typeof detail === "string") message = detail;
    else if (detail?.message) {
      message = detail.message;
      code = detail.code;
    } else if (payload?.message) message = payload.message;
  } catch {
    /* keep fallback */
  }
  throw new ApiError(message, res.status, code);
}

function networkError(): ApiError {
  return new ApiError("API tidak terjangkau — cek API hidup dan Endpoint di Pengaturan", 0, "NETWORK_ERROR");
}

export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, withAuth(init));
  } catch {
    throw networkError();
  }
}

async function safeFetch(input: string, init?: RequestInit): Promise<Response> {
  return apiFetch(input, init);
}

export async function listJobs(base: string, limit = 100): Promise<Job[]> {
  const res = await apiFetch(`${base}/api/v1/jobs?limit=${limit}`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat daftar job");
  const payload = await res.json();
  return payload.jobs ?? [];
}

export async function getJob(base: string, jobId: string): Promise<Job> {
  const res = await safeFetch(`${base}/api/v1/jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Job tidak ditemukan");
  return res.json();
}

export async function createJob(base: string, input: CreateJobInput): Promise<Job> {
  const res = await apiFetch(`${base}/api/v1/jobs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify({
      source_url: input.source_url,
      language: input.language,
      max_clips: input.max_clips,
      min_duration: input.min_duration,
      max_duration: input.max_duration,
      burn_subtitles: input.burn_subtitles,
      subtitle_style: input.subtitle_style,
      subtitle_position: input.subtitle_position,
      subtitle_max_words_per_line: input.subtitle_max_words_per_line,
      brand_enabled: input.brand_enabled,
      brand_template: input.brand_template,
      brand_channel_name: input.brand_channel_name || null,
      brand_avatar_asset: input.brand_avatar_asset || null,
      brand_display_mode: input.brand_display_mode,
      brand_position: input.brand_position,
      brand_timing: input.brand_timing,
      brand_duration: input.brand_duration,
      brand_blur: input.brand_blur,
      brand_dim: input.brand_dim,
      brand_animation: input.brand_animation,
      brand_additional_text: input.brand_additional_text || null,
    }),
  });
  if (!res.ok) await parseError(res, "URL tidak dapat diproses");
  return res.json();
}

export async function searchJobs(base: string, params: JobSearchParams): Promise<JobSearchResult> {
  const query = new URLSearchParams();
  if (params.q) query.set("q", params.q);
  if (params.status) query.set("status", params.status);
  if (params.channel) query.set("channel", params.channel);
  if (params.date_from) query.set("date_from", String(params.date_from));
  if (params.date_to) query.set("date_to", String(params.date_to));
  if (params.sort) query.set("sort", params.sort);
  query.set("page", String(params.page ?? 1));
  query.set("per_page", String(params.per_page ?? 20));
  const res = await apiFetch(`${base}/api/v1/jobs/search?${query.toString()}`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal mencari job");
  return res.json();
}

export async function listChannels(base: string): Promise<ChannelInfo[]> {
  const res = await apiFetch(`${base}/api/v1/channels`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat channel");
  const payload = await res.json();
  return payload.channels ?? [];
}

export interface BrandAsset {
  asset_id: string;
  preview_url: string;
}

export async function uploadBrandAvatar(base: string, file: File): Promise<BrandAsset> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch(`${base}/api/v1/brand-assets/avatar/upload`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) await parseError(res, "Gagal mengunggah avatar");
  return res.json();
}

export async function resolveBrandAvatar(base: string, channelName: string): Promise<BrandAsset> {
  const res = await apiFetch(`${base}/api/v1/brand-assets/avatar/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ channel_name: channelName }),
  });
  if (!res.ok) await parseError(res, "Avatar channel tidak ditemukan");
  return res.json();
}

export interface BrandChannelIdentity {
  channel_name: string | null;
  channel_id: string | null;
  asset_id: string | null;
  preview_url: string | null;
}

export async function resolveBrandAvatarUrl(
  base: string,
  url: string,
  opts?: { signal?: AbortSignal },
): Promise<BrandChannelIdentity> {
  const res = await apiFetch(`${base}/api/v1/brand-assets/avatar/resolve-url`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
    signal: opts?.signal,
  });
  if (!res.ok) await parseError(res, "Gagal mengenali channel dari URL");
  return res.json();
}

export async function refreshJobMetadata(base: string, jobId: string): Promise<{ queued: boolean }> {
  const res = await safeFetch(`${base}/api/v1/jobs/${encodeURIComponent(jobId)}/refresh-metadata`, { method: "POST" });
  if (!res.ok) await parseError(res, "Gagal meminta refresh metadata");
  return res.json();
}

export async function backfillVideos(base: string, limit = 20): Promise<{ queued: number; filled_from_cache: number }> {
  const res = await apiFetch(`${base}/api/v1/videos/backfill?limit=${limit}`, { method: "POST" });
  if (!res.ok) await parseError(res, "Gagal memulai backfill");
  return res.json();
}

export async function cancelJob(base: string, jobId: string): Promise<Job> {
  const res = await safeFetch(`${base}/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
  if (!res.ok) await parseError(res, "Job tidak dapat dibatalkan");
  return res.json();
}

export async function deleteJob(base: string, jobId: string): Promise<void> {
  const res = await safeFetch(`${base}/api/v1/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
  if (!res.ok && res.status !== 204) await parseError(res, "Job tidak dapat dihapus");
}

export interface RecreatePreview {
  job_id: string;
  source_url: string;
  video?: { title?: string | null } | null;
  status: string;
  has_transcript_artifact: boolean;
  cache_hit: boolean;
  transcript_available: boolean;
}

export async function recreatePreview(base: string, jobId: string): Promise<RecreatePreview> {
  const res = await safeFetch(`${base}/api/v1/jobs/${encodeURIComponent(jobId)}/recreate-preview`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat info recreate");
  return res.json();
}

export async function recreateJob(base: string, jobId: string, forceRetranscribe = false): Promise<Job> {
  const res = await safeFetch(`${base}/api/v1/jobs/${encodeURIComponent(jobId)}/recreate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify({ force_retranscribe: forceRetranscribe }),
  });
  if (!res.ok) await parseError(res, "Job tidak dapat di-recreate");
  return res.json();
}

export async function getServerConfig(base: string): Promise<ServerConfig> {
  const res = await apiFetch(`${base}/health/config`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat konfigurasi server");
  return res.json();
}

export async function getAppSettings(base: string): Promise<AppSetting[]> {
  const res = await apiFetch(`${base}/api/v1/settings`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat pengaturan");
  const payload = await res.json();
  return payload.settings ?? [];
}

export async function patchAppSettings(
  base: string,
  settings: Record<string, string | number | boolean>
): Promise<AppSetting[]> {
  const res = await apiFetch(`${base}/api/v1/settings`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ settings }),
  });
  if (!res.ok) await parseError(res, "Gagal menyimpan pengaturan");
  const payload = await res.json();
  return payload.settings ?? [];
}

export async function resetAppSetting(base: string, key: string): Promise<AppSetting[]> {
  const res = await apiFetch(`${base}/api/v1/settings/${encodeURIComponent(key)}`, { method: "DELETE" });
  if (!res.ok) await parseError(res, "Gagal mereset pengaturan");
  const payload = await res.json();
  return payload.settings ?? [];
}

export interface GeminiTestResult {
  ok: boolean;
  model: string;
  latency_ms?: number | null;
  message?: string | null;
  discovery?: {
    status: string;
    source?: string;
    models?: number;
    compatible?: number;
    free_tier?: number;
    kind?: string;
  } | null;
}

export async function testGemini(base: string, apiKey?: string): Promise<GeminiTestResult> {
  const res = await apiFetch(`${base}/api/v1/settings/test/gemini`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(apiKey ? { api_key: apiKey } : {}),
  });
  if (!res.ok) await parseError(res, "Tes koneksi Gemini gagal");
  return res.json();
}

export async function getAiModels(base: string): Promise<AiModelsResponse> {
  const res = await apiFetch(`${base}/api/v1/settings/ai/models`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat daftar model Gemini");
  return res.json();
}

export async function refreshAiModels(base: string): Promise<AiRefreshResult> {
  const res = await apiFetch(`${base}/api/v1/settings/ai/models/refresh`, { method: "POST" });
  if (!res.ok) await parseError(res, "Gagal me-refresh daftar model Gemini");
  return res.json();
}

export async function getAiSelection(base: string): Promise<AiSelectionResponse> {
  const res = await apiFetch(`${base}/api/v1/settings/ai/selection`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat konfigurasi chain AI");
  return res.json();
}

/* ── Gemini provider pool ── */

async function poolFetch(base: string, path: string, init?: RequestInit, fallback?: string) {
  const res = await apiFetch(`${base}${path}`, init);
  if (!res.ok) await parseError(res, fallback ?? "Operasi provider gagal");
  return res.json();
}

export const listProviders = (base: string) => poolFetch(base, "/api/v1/settings/ai/providers", undefined, "Gagal memuat provider");

export function addProvider(base: string, body: { name: string; api_key: string; enabled?: boolean; priority?: number; models?: { id: string; enabled: boolean; priority: number }[] }) {
  return poolFetch(base, "/api/v1/settings/ai/providers", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }, "Gagal menambah provider");
}

export function editProvider(base: string, id: string, body: Record<string, unknown>) {
  return poolFetch(base, `/api/v1/settings/ai/providers/${encodeURIComponent(id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }, "Gagal menyimpan provider");
}

export function deleteProvider(base: string, id: string) {
  return poolFetch(base, `/api/v1/settings/ai/providers/${encodeURIComponent(id)}`, { method: "DELETE" }, "Gagal menghapus provider");
}

export function reorderProviders(base: string, ids: string[]) {
  return poolFetch(base, "/api/v1/settings/ai/providers/reorder", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }),
  }, "Gagal menyusun ulang provider");
}

export const getProvidersHealth = (base: string) => poolFetch(base, "/api/v1/settings/ai/providers/health", undefined, "Gagal memuat health provider");

export function resetProviderHealth(base: string, providerId?: string) {
  return poolFetch(base, "/api/v1/settings/ai/providers/health/reset", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ provider_id: providerId ?? null }),
  }, "Gagal mereset health");
}

export const testProvider = (base: string, id: string) => poolFetch(base, `/api/v1/settings/ai/providers/${encodeURIComponent(id)}/test`, { method: "POST" }, "Tes koneksi provider gagal");

export function testProviderModel(base: string, id: string, model: string) {
  return poolFetch(base, `/api/v1/settings/ai/providers/${encodeURIComponent(id)}/test-model`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }),
  }, "Tes model gagal");
}

export interface FallbackChainResult {
  ok: boolean;
  success_provider?: string | null;
  success_model?: string | null;
  attempts_total?: number | null;
  fallback_used?: boolean | null;
  ordered_providers?: string[];
  simulated_failures?: string[];
  failure_history?: { provider?: string; model?: string; status?: string | number | null }[];
}

export function testFallbackChain(base: string, failProviderIds: string[]) {
  return poolFetch(base, "/api/v1/settings/ai/providers/test-fallback", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fail_provider_ids: failProviderIds }),
  }, "Tes rantai fallback gagal") as Promise<FallbackChainResult>;
}

export async function getYoutubeCookies(base: string): Promise<{ configured: boolean }> {
  const res = await apiFetch(`${base}/api/v1/settings/youtube-cookies`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat status cookies");
  return res.json();
}

export async function saveYoutubeCookies(base: string, cookies_txt: string): Promise<{ saved: boolean }> {
  const res = await apiFetch(`${base}/api/v1/settings/youtube-cookies`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookies_txt }),
  });
  if (!res.ok) await parseError(res, "Gagal menyimpan cookies");
  return res.json();
}

export interface TalkNetStatus {
  enabled: boolean;
  status: string;
  device_request?: string | null;
  resource_mode?: string | null;
  device?: string | null;
  backend?: string | null;
  device_name?: string | null;
  device_reason?: string | null;
  memory_total?: number | null;
  memory_free?: number | null;
  model_found?: boolean | null;
  model_loaded?: boolean | null;
  worker?: Record<string, string> | null;
}

export async function getTalkNetStatus(base: string): Promise<TalkNetStatus> {
  const res = await apiFetch(`${base}/api/v1/vision/talknet/status`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat status TalkNet");
  return res.json();
}

export const getProviderModels = (base: string, id: string) => poolFetch(base, `/api/v1/settings/ai/providers/${encodeURIComponent(id)}/models`, undefined, "Gagal memuat model provider");

export const refreshProviderModels = (base: string, id: string) => poolFetch(base, `/api/v1/settings/ai/providers/${encodeURIComponent(id)}/models/refresh`, { method: "POST" }, "Refresh model gagal");

export async function checkHealth(base: string): Promise<{ api: boolean; worker: boolean }> {
  let api = false;
  let worker = false;
  try {
    const live = await apiFetch(`${base}/health/live`, { cache: "no-store" });
    api = live.ok;
  } catch {
    api = false;
  }
  try {
    const ready = await apiFetch(`${base}/health/ready`, { cache: "no-store" });
    worker = ready.ok;
  } catch {
    worker = false;
  }
  return { api, worker };
}

export function clipUrl(base: string, downloadUrl: string): string {
  return `${base}${downloadUrl}`;
}

export function formatBytes(bytes?: number | null): string | null {
  if (typeof bytes !== "number" || !isFinite(bytes)) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

export { formatClock, formatDuration, formatPublished } from "./format";

export interface WorkerStatus {
  worker_up: boolean;
  active_jobs: string[];
  reserved_jobs: string[];
  note: string;
}

export async function getWorkerStatus(base: string): Promise<WorkerStatus> {
  const res = await apiFetch(`${base}/api/v1/worker/status`, { cache: "no-store" });
  if (!res.ok) await parseError(res, "Gagal memuat status worker");
  return res.json();
}

export function formatTime(ts?: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("id-ID", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
