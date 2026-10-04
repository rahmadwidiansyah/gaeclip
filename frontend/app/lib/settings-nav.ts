import type { AppSetting, GeminiProviderInfo } from "./types";

/** Settings information architecture (presentation only).
 *  Backend category strings are mapped to nav categories; no setting
 *  is renamed, moved in storage, or re-validated here. */

export type SettingsCategoryId = "general" | "ai" | "video" | "transcript" | "advanced" | "system";

export interface SettingsCategory {
  id: SettingsCategoryId;
  label: string;
  blurb: string;
  /** Backend `category` values rendered in this view (generic fields). */
  backendCategories: string[];
}

/** Gemini pool tuning knobs live in backend category "AI & Models" but
 *  are shown under the Gemini Advanced tab, not the generic form. */
export const GEMINI_ADVANCED_KEYS = [
  "GEMINI_MAX_ATTEMPTS",
  "GEMINI_RETRY_MAX_ATTEMPTS",
  "GEMINI_RETRY_BASE_DELAY_MS",
  "GEMINI_RETRY_MAX_DELAY_MS",
  "GEMINI_RETRY_JITTER_RATIO",
  "GEMINI_REQUEST_TIMEOUT_MS",
  "GEMINI_CIRCUIT_FAILURE_THRESHOLD",
  "GEMINI_CIRCUIT_COOLDOWN_MS",
  "GEMINI_AUTH_COOLDOWN_MS",
  "GEMINI_MAX_CONCURRENT_PER_PROVIDER",
  "GEMINI_ROUTING_STRATEGY",
  "GEMINI_MODEL_DISCOVERY_TTL",
];

export const SETTINGS_CATEGORIES: SettingsCategory[] = [
  { id: "general", label: "Umum", blurb: "Koneksi & preferensi aplikasi", backendCategories: [] },
  { id: "ai", label: "AI & Gemini", blurb: "Provider, model & fallback", backendCategories: ["AI & Models"] },
  { id: "video", label: "Video & Rendering", blurb: "Kualitas & output", backendCategories: ["Processing"] },
  { id: "transcript", label: "Transkripsi", blurb: "Bahasa & subtitle", backendCategories: ["Transcript & LLM"] },
  { id: "advanced", label: "Lanjutan", blurb: "Penyimpanan, log & tuning", backendCategories: ["Storage & Retention", "Logging"] },
  { id: "system", label: "Sistem", blurb: "Status & info runtime", backendCategories: [] },
];

export type GeminiTabId = "overview" | "providers" | "models" | "fallback" | "advanced";

export const GEMINI_TABS: { id: GeminiTabId; label: string }[] = [
  { id: "overview", label: "Ringkasan" },
  { id: "providers", label: "Provider" },
  { id: "models", label: "Model" },
  { id: "fallback", label: "Fallback" },
  { id: "advanced", label: "Lanjutan" },
];

export interface SearchHit {
  category: SettingsCategoryId;
  geminiTab?: GeminiTabId;
  title: string;
  detail: string;
}

interface SearchableField {
  key: string;
  label: string;
  description: string;
  category: string;
}

/** Flat search across categories, generic fields, and Gemini areas. */
export function searchSettings(
  query: string,
  fields: SearchableField[],
  geminiTabForKey: (key: string) => GeminiTabId | undefined,
  categoryForBackend: (backend: string) => SettingsCategoryId,
): SearchHit[] {
  const q = query.trim().toLowerCase();
  if (q.length < 2) return [];
  const hits: SearchHit[] = [];
  const push = (category: SettingsCategoryId, title: string, detail: string, geminiTab?: GeminiTabId) => {
    if (hits.some((h) => h.title === title && h.category === category)) return;
    hits.push({ category, title, detail, geminiTab });
  };
  for (const c of SETTINGS_CATEGORIES) {
    if (c.label.toLowerCase().includes(q) || c.blurb.toLowerCase().includes(q)) {
      push(c.id, c.label, c.blurb);
    }
  }
  for (const f of fields) {
    if (f.label.toLowerCase().includes(q) || f.description.toLowerCase().includes(q) || f.key.toLowerCase().includes(q)) {
      push(categoryForBackend(f.category), f.label, f.key, geminiTabForKey(f.key));
    }
  }
  const geminiAreas: { tab: GeminiTabId; title: string; detail: string; keys: string[] }[] = [
    { tab: "overview", title: "Status Gemini", detail: "Provider · model · fallback", keys: ["gemini", "status", "ready", "provider", "siap"] },
    { tab: "providers", title: "Provider Gemini", detail: "Tambah / uji / atur prioritas API", keys: ["gemini", "provider", "api key", "priority", "prioritas", "test connection", "uji koneksi"] },
    { tab: "models", title: "Model Gemini", detail: "Discovery & pemilihan model", keys: ["model", "discovery", "penemuan", "available", "tersedia"] },
    { tab: "fallback", title: "Urutan Fallback", detail: "Urutan provider × model", keys: ["fallback", "order", "urutan", "retry", "percobaan"] },
    { tab: "advanced", title: "Gemini Lanjutan", detail: "Retry, circuit breaker, timeout", keys: ["retry", "circuit", "timeout", "batas waktu", "cooldown", "pendinginan", "backoff", "lanjutan", "advanced"] },
  ];
  for (const area of geminiAreas) {
    if (area.keys.some((k) => k.includes(q) || q.includes(k)) || area.title.toLowerCase().includes(q)) {
      push("ai", area.title, area.detail, area.tab);
    }
  }
  return hits.slice(0, 12);
}

/** Configured P1/M1 → P1/M2 … fallback order for visualization. */
export function fallbackOrder(providers: GeminiProviderInfo[]): { providerId: string; providerName: string; modelId: string; step: number }[] {
  const ordered = [...providers.filter((p) => p.enabled)].sort((a, b) => a.priority - b.priority || (a.id < b.id ? -1 : 1));
  const steps: { providerId: string; providerName: string; modelId: string; step: number }[] = [];
  for (const p of ordered) {
    const models = [...p.models]
      .filter((m) => m.enabled && !p.unavailable_models.includes(m.id))
      .sort((a, b) => a.priority - b.priority || (a.id < b.id ? -1 : 1));
    for (const m of models) {
      steps.push({ providerId: p.id, providerName: p.name, modelId: m.id, step: steps.length + 1 });
    }
  }
  return steps;
}

/** Split generic fields: Gemini advanced knobs vs the rest. */
export function splitGeminiAdvanced(fields: AppSetting[]): { advanced: AppSetting[]; rest: AppSetting[] } {
  const advanced: AppSetting[] = [];
  const rest: AppSetting[] = [];
  for (const f of fields) {
    (GEMINI_ADVANCED_KEYS.includes(f.key) ? advanced : rest).push(f);
  }
  return { advanced, rest };
}
