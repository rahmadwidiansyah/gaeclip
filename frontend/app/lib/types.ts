export type JobStatus =
  | "accepted"
  | "queued"
  | "running"
  | "retrying"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "expired";

export interface JobError {
  code?: string;
  message?: string;
  retryable?: boolean;
}

export interface JobClip {
  clip_id: string;
  title?: string;
  score?: number;
  reason?: string;
  duration_seconds?: number | null;
  size_bytes?: number | null;
  download_url: string;
  description?: string | null;
  hashtags?: string[] | null;
  metadata_source?: string | null;
  metadata_updated_at?: number | null;
}

export interface ClipTask {
  clip_id: string;
  title?: string;
  status: string;
  stage_detail?: string | null;
  error?: JobError | null;
  score?: number;
  reason?: string;
  duration_seconds?: number | null;
  size_bytes?: number | null;
  attempt?: number;
}

export interface FallbackChainEntry {
  level?: string | null;
  status?: string | null;
  reason?: string | null;
  candidates?: number | null;
  best_score?: number | null;
  threshold?: number | null;
  provider?: string | null;
  model?: string | null;
}

export interface JobCuration {
  source?: string | null;
  provider?: string | null;
  provider_id?: string | null;
  provider_name?: string | null;
  model?: string | null;
  attempts_total?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  attempted_provider?: string | null;
  fallback_reason?: string | null;
  heatmap_used?: boolean | null;
  curation_method?: "ai" | "heatmap" | "heuristic" | "deterministic" | null;
  ai_used?: boolean | null;
  fallback_used?: boolean | null;
  score_breakdown?: Record<string, Record<string, number>> | null;
  fallback_chain?: FallbackChainEntry[] | null;
  algorithm_version?: string | null;
  candidate_count?: number | null;
  runtime_ms?: number | null;
  best_score?: number | null;
  heatmap_stats?: Record<string, number> | null;
  duplicates_removed?: number | null;
  overlap_rejected?: number | null;
  transcript_hash?: string | null;
}

export interface VideoMeta {
  youtube_video_id: string;
  url: string;
  title?: string | null;
  channel_name?: string | null;
  thumbnail_url?: string | null;
  published_at?: string | null;
  duration?: number | null;
}

export interface Job {
  job_id: string;
  source_url: string;
  language: string;
  video_id?: string | null;
  video?: VideoMeta | null;
  status: JobStatus | string;
  stage: string;
  progress: number;
  stage_detail?: string | null;
  current_clip?: number;
  total_clips?: number;
  max_clips?: number;
  min_duration?: number;
  max_duration?: number;
  burn_subtitles?: boolean;
  subtitle_style?: SubtitleStyle;
  subtitle_position?: SubtitlePosition;
  subtitle_max_words_per_line?: number;
  curation?: JobCuration | null;
  error?: JobError | null;
  clips?: JobClip[];
  clip_tasks?: ClipTask[] | null;
  recreated_from_job_id?: string | null;
  transcript_reused?: boolean | null;
  transcript_cache_id?: string | null;
  force_retranscribe?: boolean | null;
  created_at?: number;
  updated_at?: number;
}

export interface JobSearchParams {
  q?: string;
  status?: string;
  channel?: string;
  date_from?: number;
  date_to?: number;
  sort?: string;
  page?: number;
  per_page?: number;
}

export interface JobSearchResult {
  jobs: Job[];
  total: number;
  page: number;
  per_page: number;
}

export interface ChannelInfo {
  name: string;
  jobs: number;
  avatar_url?: string | null;
}

export type SubtitleStyle = "bold_pop" | "dynamic_highlight" | "casual_center" | "elegant_serif";
export type SubtitlePosition = "center" | "bottom_center" | "bottom";

export const SUBTITLE_STYLES: { id: SubtitleStyle; label: string }[] = [
  { id: "bold_pop", label: "Bold Pop" },
  { id: "dynamic_highlight", label: "Dynamic Highlight" },
  { id: "casual_center", label: "Casual Center" },
  { id: "elegant_serif", label: "Elegant Serif" },
];

export const SUBTITLE_POSITIONS: { id: SubtitlePosition; label: string }[] = [
  { id: "center", label: "Center" },
  { id: "bottom_center", label: "Bottom Center" },
  { id: "bottom", label: "Bottom" },
];

export interface CreateJobInput {
  source_url: string;
  language: string;
  max_clips: number;
  min_duration: number;
  max_duration: number;
  burn_subtitles: boolean;
  subtitle_style: SubtitleStyle;
  subtitle_position: SubtitlePosition;
  subtitle_max_words_per_line: number;
  brand_enabled: boolean;
  brand_template: string;
  brand_channel_name: string;
  brand_avatar_asset: string;
  brand_display_mode: string;
  brand_position: string;
  brand_timing: string;
  brand_duration: number;
  brand_blur: number;
  brand_dim: number;
  brand_animation: string;
  brand_additional_text: string;
}

export interface ServerConfig {
  max_clips_default: number;
  max_clips_limit: number;
  min_clip_duration: number;
  max_clip_duration: number;
  max_source_duration: number;
  whisper_model: string;
  gemini_model: string;
  llm_available: boolean;
  runtime?: string;
  runner?: string;
  system?: {
    redis_connected: boolean;
    storage_root: string;
    storage_free_bytes: number | null;
    frontend_origins: string[];
    public_mode: boolean;
    api_host: string;
    api_port: number;
  };
}

export interface AppSetting {
  key: string;
  type: "string" | "integer" | "float" | "boolean" | "secret" | "duration" | "file_size" | "enum";
  category: string;
  label: string;
  env_var: string;
  description: string;
  unit: string | null;
  options: string[];
  minimum: number | null;
  maximum: number | null;
  needs_restart: boolean;
  apply_note: string;
  default: string | number | boolean | null;
  value: string | number | boolean | null;
  configured: boolean;
  customized: boolean;
}

/* ── Gemini model discovery (backend-driven; never hardcoded) ── */

export interface AiModelInfo {
  id: string;
  display_name: string;
  provider: string;
  available: boolean;
  supports_generate_content: boolean;
  family: string;
  generation: string;
  variant: string;
  tier: string;
  release_channel: "stable" | "preview" | "experimental" | "latest_alias" | "unknown" | string;
  is_preview: boolean;
  is_experimental: boolean;
  is_latest_alias: boolean;
  category: string;
  free_tier: "free" | "paid" | "unknown" | string;
  free_tier_source: string;
  eligible_for_gaeclip: boolean;
  input_token_limit?: number | null;
  output_token_limit?: number | null;
  version?: string;
  description?: string;
}

export interface AiModelsResponse {
  provider: string;
  status: string;
  source: "live" | "cache" | "cache-stale" | string;
  discovered_at: number;
  expires_at: number;
  counts: { discovered: number; compatible: number; free_tier: number; unsupported: number };
  models: AiModelInfo[];
}

export interface AiRefreshResult extends AiModelsResponse {
  discovered: number;
  compatible: number;
  free_tier: number;
  unsupported: number;
  selection: AiSelection;
}

export interface AiChainEntry {
  id: string;
  available: boolean | null;
  eligible: boolean | null;
  warning: string | null;
}

export interface AiSelection {
  mode: string;
  primary: string | null;
  fallbacks: string[];
  method: string;
  error?: string;
  models: AiChainEntry[];
}

export interface AiSelectionResponse {
  provider: string;
  prefs: { mode: string; primary_model: string; fallback_models: string[]; free_tier_only: boolean };
  selection: AiSelection;
  registry: { source: string; counts: Record<string, number> };
}

export const TERMINAL_STATES = ["completed", "partial", "failed", "cancelled", "expired"];
export const ACTIVE_STATES = ["accepted", "queued", "running", "retrying"];

/* ── Gemini provider pool (multi key × multi model; keys never leave backend) ── */

export interface ProviderModelEntry {
  id: string;
  enabled: boolean;
  priority: number;
}

export interface ProviderHealth {
  state: string;
  failures: number;
  successes: number;
  cooldown_until: number;
  circuit: string;
  last_success: number | null;
  last_error: string | null;
  last_error_category: string | null;
}

export interface GeminiProviderInfo {
  id: string;
  name: string;
  enabled: boolean;
  priority: number;
  key_suffix: string;
  key_configured: boolean;
  models: ProviderModelEntry[];
  unavailable_models: string[];
  model_mode?: string;
  auto_depth?: number;
  auto_free_tier_only?: boolean;
  health?: ProviderHealth;
  success_rate?: number | null;
  models_health?: Record<string, ProviderHealth>;
}

export interface ProvidersResponse {
  providers: GeminiProviderInfo[];
}

export interface ProviderDiscoveryModel extends AiModelInfo {
  configured: boolean;
  marked_unavailable: boolean;
}

export const STAGE_ORDER = [
  "accepted",
  "enqueueing",
  "processing",
  "metadata",
  "subtitles",
  "transcribing",
  "curating",
  "downloading",
  "rendering",
];

export const STAGE_LABELS: Record<string, { label: string; detail: string }> = {
  accepted: { label: "Diterima", detail: "Permintaan masuk, menunggu worker." },
  enqueueing: { label: "Antre", detail: "Job dikirim ke worker." },
  processing: { label: "Diproses", detail: "Worker memeriksa video." },
  metadata: { label: "Metadata", detail: "Membaca durasi & info video." },
  subtitles: { label: "Subtitle", detail: "Mengunduh subtitle." },
  transcribing: { label: "Transkripsi", detail: "Transkripsi audio dengan Whisper." },
  curating: { label: "Kurasi", detail: "Memilih hook terbaik." },
  downloading: { label: "Unduh", detail: "Mengambil potongan video." },
  rendering: { label: "Render", detail: "Render 9:16 + subtitle." },
  completed: { label: "Selesai", detail: "Semua clip siap diunduh." },
  partial: { label: "Sebagian", detail: "Sebagian clip berhasil." },
  failed: { label: "Gagal", detail: "Proses berhenti karena error." },
  cancelled: { label: "Batal", detail: "Job dibatalkan." },
  expired: { label: "Kedaluwarsa", detail: "Artifact tidak tersedia." },
};
