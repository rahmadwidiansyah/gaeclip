"use client";

import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Collapsible, ConfirmDialog, PageHeader, SearchInput } from "../components/ui";
import AiModelsSection from "./ai-models";
import AiProvidersSection from "./ai-providers";
import {
  checkHealth,
  formatBytes,
  getAiSelection,
  getAppSettings,
  getServerConfig,
  getTalkNetStatus,
  getYoutubeCookies,
  listProviders,
  patchAppSettings,
  resetAppSetting,
  saveYoutubeCookies,
  testFallbackChain,
  testGemini,
  testProvider,
} from "../lib/api";
import {
  GEMINI_ADVANCED_KEYS,
  GEMINI_TABS,
  SETTINGS_CATEGORIES,
  fallbackOrder,
  searchSettings,
  splitGeminiAdvanced,
  type GeminiTabId,
  type SettingsCategoryId,
} from "../lib/settings-nav";
import { useSettings, useToast } from "../lib/store";
import type { AppSetting, GeminiProviderInfo, ServerConfig } from "../lib/types";
import type { TalkNetStatus } from "../lib/api";

/* AI chain prefs live in the dedicated backend-driven section below. */
const AI_PREF_KEYS = ["AI_MODEL_MODE", "AI_PRIMARY_MODEL", "AI_FALLBACK_MODELS", "AI_FREE_TIER_ONLY"];

/* ── unit helpers (display <-> base unit) ── */

const SECOND_UNITS = [
  { id: "detik", label: "detik", factor: 1 },
  { id: "menit", label: "menit", factor: 60 },
  { id: "jam", label: "jam", factor: 3600 },
];
const HOUR_UNITS = [
  { id: "jam", label: "jam", factor: 1 },
  { id: "hari", label: "hari", factor: 24 },
];
const BYTE_UNITS = [
  { id: "MB", label: "MB", factor: 1048576 },
  { id: "GB", label: "GB", factor: 1073741824 },
];

function unitOptions(unit: string | null) {
  if (unit === "hours") return HOUR_UNITS;
  if (unit === "bytes") return BYTE_UNITS;
  return SECOND_UNITS;
}

function splitUnit(base: number, unit: string | null): { num: number; unit: string } {
  if (unit === "hours") {
    if (Number.isInteger(base) && base >= 24 && base % 24 === 0) return { num: base / 24, unit: "hari" };
    return { num: base, unit: "jam" };
  }
  if (unit === "bytes") {
    if (base >= 1073741824) return { num: Math.round((base / 1073741824) * 100) / 100, unit: "GB" };
    return { num: Math.round((base / 1048576) * 100) / 100, unit: "MB" };
  }
  if (base >= 3600 && base % 3600 === 0) return { num: base / 3600, unit: "jam" };
  if (base >= 60 && base % 60 === 0 && Number.isInteger(base)) return { num: base / 60, unit: "menit" };
  return { num: base, unit: "detik" };
}

function UnitField({
  setting,
  value,
  onChange,
  error,
}: {
  setting: AppSetting;
  value: number;
  onChange: (base: number) => void;
  error?: string | null;
}) {
  const plainCount = setting.unit == null;
  const opts = unitOptions(setting.unit);
  const [unitId, setUnitId] = useState(() => splitUnit(value, setting.unit).unit);
  if (plainCount) {
    return (
      <div>
        <input
          type="number"
          step="1"
          min={0}
          value={Number.isNaN(value) ? "" : value}
          onChange={(e) => {
            const n = Number(e.target.value);
            onChange(Number.isNaN(n) ? NaN : n);
          }}
          aria-label={setting.label}
        />
        {error && (
          <p className="field-err" role="alert">
            {error}
          </p>
        )}
      </div>
    );
  }
  const shown = (() => {
    const opt = opts.find((o) => o.id === unitId) ?? opts[0];
    const v = value / opt.factor;
    return Number.isInteger(v) ? v : Math.round(v * 100) / 100;
  })();

  return (
    <div>
      <div className="unit-row">
        <input
          type="number"
          step="any"
          min={0}
          value={Number.isNaN(shown) ? "" : shown}
          onChange={(e) => {
            const opt = opts.find((o) => o.id === unitId) ?? opts[0];
            const n = Number(e.target.value);
            onChange(Number.isNaN(n) ? NaN : n * opt.factor);
          }}
          aria-label={setting.label}
        />
        <select value={unitId} onChange={(e) => setUnitId(e.target.value)} aria-label="Satuan">
          {opts.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {error && (
        <p className="field-err" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

/* ── settings IA: presentational building blocks (logic stays below) ── */

function GenericFields({
  fields,
  effectiveValue,
  renderField,
  fieldErrors,
  onResetKey,
}: {
  fields: AppSetting[];
  effectiveValue: (e: AppSetting) => number | string;
  renderField: (e: AppSetting) => React.ReactNode;
  fieldErrors: Record<string, string>;
  onResetKey: (key: string) => void;
}) {
  if (fields.length === 0) return <p className="muted">Tidak ada pengaturan di sini.</p>;
  return (
    <div className="set-grid">
      {fields.map((e) => (
        <div key={e.key} className="set-field">
          <div className="set-label">
            <label>{e.label}</label>
            {e.needs_restart && <span className="mini-chip">perlu restart worker</span>}
            {e.customized && <span className="mini-chip mini-chip-active">kustom</span>}
          </div>
          {renderField(e)}
          <small className="muted">
            {e.description}
            <br />
            Variabel lingkungan: <code className="mono-sm">{e.env_var}</code>
          </small>
          <div className="set-foot">
            <small className="muted">{e.apply_note}</small>
            {e.customized && (
              <button type="button" className="btn btn-text btn-sm" onClick={() => onResetKey(e.key)}>
                Atur Ulang
              </button>
            )}
          </div>
          {fieldErrors[e.key] ? (
            <p className="field-err" role="alert">
              {fieldErrors[e.key]}
            </p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function CategoryNav({
  active,
  onPick,
}: {
  active: SettingsCategoryId;
  onPick: (id: SettingsCategoryId) => void;
}) {
  return (
    <nav className="settings-nav card" aria-label="Kategori pengaturan">
      {SETTINGS_CATEGORIES.map((c) => (
        <button key={c.id} type="button" onClick={() => onPick(c.id)} aria-current={active === c.id ? "page" : undefined}>
          <strong>{c.label}</strong>
          <small>{c.blurb}</small>
        </button>
      ))}
    </nav>
  );
}

function YoutubeCookiesCard({
  apiBase,
  push,
}: {
  apiBase: string;
  push: (message: string, kind?: "info" | "success" | "error") => void;
}) {
  const [text, setText] = useState("");
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getYoutubeCookies(apiBase)
      .then((res) => { if (!cancelled) setConfigured(res.configured); })
      .catch(() => { if (!cancelled) setConfigured(null); });
    return () => { cancelled = true; };
  }, [apiBase]);

  const onSave = async () => {
    setSaving(true);
    try {
      await saveYoutubeCookies(apiBase, text);
      setConfigured(true);
      setText("");
      push("Cookies YouTube tersimpan", "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal menyimpan cookies", "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="card" aria-label="Cookies YouTube">
      <div className="card-head">
        <h2>YouTube Cookies</h2>
        <p>
          {configured
            ? "File cookies terkonfigurasi."
            : "Belum ada file cookies."}{" "}
          Tempeli isi cookies.txt format Netscape dari browser yang login YouTube.
        </p>
      </div>
      <div className="form">
        <div className="field">
          <label htmlFor="yt-cookies">Isi cookies.txt</label>
          <textarea
            id="yt-cookies"
            rows={4}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\t…"}
            spellCheck={false}
          />
        </div>
        <div>
          <button type="button" className="btn btn-tonal btn-sm" onClick={onSave} disabled={saving || !text.trim()}>
            {saving ? "Menyimpan…" : "Simpan Cookies"}
          </button>
        </div>
      </div>
    </section>
  );
}

function GeminiSection({
  apiBase,
  apiKeyVersion,
  push,
  tab,
  setTab,
  advancedFields,
  genericFieldProps,
}: {
  apiBase: string;
  apiKeyVersion: number;
  push: (message: string, kind?: "info" | "success" | "error") => void;
  tab: GeminiTabId;
  setTab: (t: GeminiTabId) => void;
  advancedFields: AppSetting[];
  genericFieldProps: {
    effectiveValue: (e: AppSetting) => number | string;
    renderField: (e: AppSetting) => React.ReactNode;
    fieldErrors: Record<string, string>;
    onResetKey: (key: string) => void;
  };
}) {
  const [providers, setProviders] = useState<GeminiProviderInfo[] | null>(null);
  const [chain, setChain] = useState<string>("");
  const [lastActive, setLastActive] = useState<number | null>(null);
  const [testingAll, setTestingAll] = useState(false);
  const [testingChain, setTestingChain] = useState(false);
  const [chainResult, setChainResult] = useState<null | {
    ok: boolean;
    success_provider?: string | null;
    success_model?: string | null;
    attempts_total?: number | null;
    ordered?: string[];
    failed?: string[];
  }>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [prov, sel] = await Promise.all([listProviders(apiBase), getAiSelection(apiBase).catch(() => null)]);
        if (cancelled) return;
        setProviders(prov.providers ?? []);
        const selection = sel?.selection;
        if (selection?.primary) {
          setChain([selection.primary, ...(selection.fallbacks ?? [])].join(" → "));
        }
      } catch {
        if (!cancelled) setProviders(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiBase, apiKeyVersion]);

  useEffect(() => {
    let max: number | null = null;
    for (const p of providers ?? []) {
      const t = p.health?.last_success;
      if (typeof t === "number" && (max === null || t > max)) max = t;
    }
    setLastActive(max);
  }, [providers]);

  const enabledProviders = (providers ?? []).filter((p) => p.enabled && p.key_configured);
  const totalModels = (providers ?? []).reduce((n, p) => n + p.models.filter((m) => m.enabled).length, 0);
  const steps = fallbackOrder(providers ?? []);

  const onTestFallbackChain = async () => {
    const ranked = [...(providers ?? [])]
      .filter((x) => x.enabled && x.key_configured)
      .sort((a, b) => a.priority - b.priority);
    if (ranked.length === 0) return;
    setTestingChain(true);
    setChainResult(null);
    try {
      // Buktikan P2 benar-benar dipanggil: P1 gagal sintetis (tanpa kuota),
      // provider berikutnya menerima request NYATA.
      const res = await testFallbackChain(apiBase, [ranked[0].id]);
      setChainResult({
        ok: true,
        success_provider: res.success_provider,
        success_model: res.success_model,
        attempts_total: res.attempts_total,
        ordered: res.ordered_providers,
        failed: res.simulated_failures,
      });
      push(`Fallback terbukti: ${ranked[0].name} gagal → ${res.success_provider} berhasil (${res.attempts_total} upaya)`, "success");
    } catch (e) {
      setChainResult({ ok: false });
      push(`Uji fallback: ${e instanceof Error ? e.message : "gagal"}`, "error");
    } finally {
      setTestingChain(false);
    }
  };

  const onTestAll = async () => {
    if (!providers || providers.length === 0) return;
    setTestingAll(true);
    try {
      let ok = 0;
      const problems: string[] = [];
      for (const p of providers.filter((x) => x.enabled && x.key_configured)) {
        try {
          await testProvider(apiBase, p.id);
          ok += 1;
        } catch (e) {
          problems.push(`${p.name}: ${e instanceof Error ? e.message : "gagal"}`);
        }
      }
      if (problems.length === 0) push(`Semua provider OK (${ok})`, "success");
      else push(`OK: ${ok}, gagal: ${problems.join("; ")}`, "error");
    } finally {
      setTestingAll(false);
    }
  };

  return (
    <div>
      <div className="gem-tabs" role="tablist" aria-label="Gemini sections">
        {GEMINI_TABS.map((t) => (
          <button key={t.id} type="button" role="tab" aria-selected={tab === t.id} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <section className="card" aria-label="Status Gemini">
          <div className="card-head">
            <h2>Status Gemini</h2>
            <p>Ringkasan provider, model, dan fallback.</p>
          </div>
          {providers === null ? (
            <p className="muted">Memuat…</p>
          ) : enabledProviders.length === 0 ? (
            <>
              <p>
                <span className="dot dot-err">Belum dikonfigurasi</span>
              </p>
              <p className="muted">Belum ada provider aktif. Tambahkan API key untuk mengaktifkan kurasi AI.</p>
              <button type="button" className="btn btn-filled btn-sm" onClick={() => setTab("providers")}>
                Tambah Provider Gemini
              </button>
            </>
          ) : (
            <>
              <p>
                <span className="dot dot-ok">Siap</span>
              </p>
              <ul className="kv">
                <li>
                  <span>Provider</span>
                  <span className="mono-sm">{enabledProviders.length} terkonfigurasi</span>
                </li>
                <li>
                  <span>Model aktif</span>
                  <span className="mono-sm">{totalModels}</span>
                </li>
                <li>
                  <span>Fallback</span>
                  <span className="mono-sm">{steps.length > 1 ? `Aktif (${steps.length} langkah)` : "Satu endpoint"}</span>
                </li>
                <li>
                  <span>Rantai aktif</span>
                  <span className="mono-sm">{chain || "—"}</span>
                </li>
                <li>
                  <span>Terakhir berhasil</span>
                  <span className="mono-sm">{lastActive ? new Date(lastActive * 1000).toLocaleString("id-ID") : "—"}</span>
                </li>
              </ul>
              <div className="fb-actions">
                <button type="button" className="btn btn-tonal btn-sm" onClick={onTestAll} disabled={testingAll || testingChain}>
                  {testingAll ? "Menguji…" : "Uji Semua Provider"}
                </button>
                <button type="button" className="btn btn-tonal btn-sm" onClick={onTestFallbackChain} disabled={testingAll || testingChain} title="P1 gagal sintetis (tanpa kuota), provider berikutnya menerima request nyata">
                  {testingChain ? "Menguji…" : "Uji Rantai Fallback"}
                </button>
              </div>
              {chainResult && (
                <ul className="kv" style={{ marginTop: 8 }}>
                  <li>
                    <span>Hasil fallback</span>
                    <span className="mono-sm">
                      {chainResult.ok
                        ? `✓ ${chainResult.failed?.[0] ?? "P1"} gagal → ${chainResult.success_provider} (${chainResult.success_model}) · ${chainResult.attempts_total} upaya`
                        : "✕ Rantai gagal — lihat log worker"}
                    </span>
                  </li>
                  {chainResult.ordered && (
                    <li>
                      <span>Urutan coba</span>
                      <span className="mono-sm">{chainResult.ordered.join(" → ")}</span>
                    </li>
                  )}
                </ul>
              )}
            </>
          )}
        </section>
      )}

      {tab === "providers" && <AiProvidersSection apiBase={apiBase} push={push} />}

      {tab === "models" && <AiModelsSection apiBase={apiBase} apiKeyVersion={apiKeyVersion} push={push} />}

      {tab === "fallback" && (
        <section className="card" aria-label="Urutan fallback">
          <div className="card-head">
            <h2>Urutan Fallback</h2>
            <p>Permintaan akan dicoba berdasarkan urutan prioritas. Model atau provider berikutnya akan digunakan jika percobaan saat ini gagal.</p>
          </div>
          {steps.length === 0 ? (
            <p className="muted">Belum ada endpoint aktif — tambah dan aktifkan model di tab Provider.</p>
          ) : (
            <ol className="fb-steps">
              {steps.map((s) => (
                <li key={`${s.providerId}/${s.modelId}`} data-step={s.step}>
                  <strong>{s.providerName}</strong>
                  <br />
                  <span className="mono-sm">└── {s.modelId}</span>
                  {s.step < steps.length && (
                    <small className="muted">
                      <br />↓ bila gagal
                    </small>
                  )}
                </li>
              ))}
            </ol>
          )}
        </section>
      )}

      {tab === "advanced" && (
        <section className="card" aria-label="Gemini lanjutan">
          <div className="card-head">
            <h2>Gemini Lanjutan</h2>
            <p>Percobaan ulang, batas waktu, circuit breaker, dan discovery. Default aman — ubah bila perlu.</p>
          </div>
          <GenericFields
            fields={advancedFields}
            effectiveValue={genericFieldProps.effectiveValue}
            renderField={genericFieldProps.renderField}
            fieldErrors={genericFieldProps.fieldErrors}
            onResetKey={genericFieldProps.onResetKey}
          />
        </section>
      )}
    </div>
  );
}

/* ── page ── */

export default function SettingsPage() {
  const { apiBase, pollMs, theme, setApiBase, setPollMs, setTheme, reset } = useSettings();
  const push = useToast();

  const [config, setConfig] = useState<ServerConfig | null>(null);
  const [health, setHealth] = useState<{ api: boolean; worker: boolean } | null>(null);
  const [entries, setEntries] = useState<AppSetting[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [baseInput, setBaseInput] = useState(apiBase);
  const [testing, setTesting] = useState(false);

  const [draft, setDraft] = useState<Record<string, number | string>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [secretMode, setSecretMode] = useState<"keep" | "replace" | "remove">("keep");
  const [secretDraft, setSecretDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [testMsg, setTestMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [apiKeyVersion, setApiKeyVersion] = useState(0);
  const [category, setCategory] = useState<SettingsCategoryId>("general");
  const [geminiTab, setGeminiTab] = useState<GeminiTabId>("overview");
  const [query, setQuery] = useState("");
  const [hitsOpen, setHitsOpen] = useState(false);

  const load = async (base: string) => {
    setLoading(true);
    try {
      const [cfg, h, list] = await Promise.all([
        getServerConfig(base),
        checkHealth(base),
        getAppSettings(base).catch(() => null),
      ]);
      setConfig(cfg);
      setHealth(h);
      if (list) {
        setEntries(list);
        setDraft({});
        setSecretMode("keep");
        setSecretDraft("");
      }
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal memuat konfigurasi", "error");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(apiBase);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase]);

  const byKey = useMemo(() => {
    const map: Record<string, AppSetting> = {};
    for (const e of entries ?? []) map[e.key] = e;
    return map;
  }, [entries]);

  const secretEntry = byKey["GEMINI_API_KEY"];
  // Legacy compat: kunci/model/timeout lama (tetap dibaca runtime sebagai
  // fallback bila Providers kosong). Whisper ke tab Transkripsi.
  const LEGACY_KEYS = ["GEMINI_MODEL", "GEMINI_TIMEOUT_SECONDS"];
  const legacyFields = useMemo(
    () => (entries ?? []).filter((e) => LEGACY_KEYS.includes(e.key)),
    [entries]
  );
  const TALKNET_KEYS = ["TALKNET_ENABLED", "TALKNET_DEVICE", "TALKNET_RESOURCE_MODE"];
  const talknetFields = useMemo(
    () => (entries ?? []).filter((e) => TALKNET_KEYS.includes(e.key)),
    [entries]
  );
  const [talknetStatus, setTalknetStatus] = useState<TalkNetStatus | null>(null);
  useEffect(() => {
    let cancelled = false;
    getTalkNetStatus(apiBase)
      .then((res) => { if (!cancelled) setTalknetStatus(res); })
      .catch(() => { if (!cancelled) setTalknetStatus(null); });
    return () => { cancelled = true; };
  }, [apiBase]);
  const nonSecret = useMemo(
    () => (entries ?? []).filter((e) => e.type !== "secret" && !AI_PREF_KEYS.includes(e.key)),
    [entries]
  );

  const effectiveValue = (e: AppSetting): number | string => {
    if (e.key in draft) return draft[e.key];
    return (e.value ?? e.default ?? "") as number | string;
  };

  const validateField = (e: AppSetting, v: number | string): string | null => {
    if (e.type === "enum" || e.type === "string") {
      if (e.type === "string" && String(v).trim() === "") return "Tidak boleh kosong";
      if (e.type === "enum" && !e.options.includes(String(v))) return "Nilai tidak dikenal";
      return null;
    }
    const n = Number(v);
    if (!isFinite(n)) return "Harus angka";
    if (e.minimum != null && n < e.minimum) return `Minimal ${e.minimum}`;
    if (e.maximum != null && n > e.maximum) return `Maksimal ${e.maximum}`;
    return null;
  };

  const dirtyKeys = useMemo(() => {
    if (!entries) return [];
    const out: string[] = [];
    for (const e of nonSecret) {
      if (!(e.key in draft)) continue;
      const server = Number(e.value ?? e.default ?? 0);
      const cur = e.type === "string" || e.type === "enum" ? String(draft[e.key]) : Number(draft[e.key]);
      const same = e.type === "string" || e.type === "enum" ? cur === String(e.value ?? e.default ?? "") : cur === server;
      if (!same) out.push(e.key);
    }
    return out;
  }, [entries, nonSecret, draft]);

  const dirty = dirtyKeys.length > 0 || secretMode !== "keep";
  const hasErrors = Object.values(fieldErrors).some(Boolean);

  const setDraftValue = (e: AppSetting, v: number | string) => {
    setDraft((d) => ({ ...d, [e.key]: v }));
    setFieldErrors((errs) => ({ ...errs, [e.key]: validateField(e, v) ?? "" }));
  };

  const onSave = async () => {
    if (!entries) return;
    setSaving(true);
    setSaveError(null);
    try {
      // client-side validation first (inside try: any unexpected error
      // must surface instead of silently killing the save)
      const errs: Record<string, string> = {};
      for (const key of dirtyKeys) {
        const e = byKey[key];
        if (!e) throw new Error(`Field tidak dikenal: ${key}`);
        const msg = validateField(e, draft[key]);
        if (msg) errs[key] = msg;
      }
      if (secretMode === "replace" && secretDraft.trim() === "") {
        push("API key pengganti masih kosong", "error");
        return;
      }
      setFieldErrors(errs);
      if (Object.values(errs).some(Boolean)) {
        push("Periksa field yang ditandai merah", "error");
        return;
      }
      const bulk: Record<string, string | number | boolean> = {};
      for (const key of dirtyKeys) {
        const e = byKey[key];
        bulk[key] = e.type === "string" || e.type === "enum" ? String(draft[key]) : Number(draft[key]);
      }
      if (secretMode === "replace") bulk["GEMINI_API_KEY"] = secretDraft.trim();
      if (Object.keys(bulk).length > 0) {
        await patchAppSettings(apiBase, bulk);
      }
      if (secretMode === "remove") {
        await resetAppSetting(apiBase, "GEMINI_API_KEY");
      }
      if (secretMode !== "keep") setApiKeyVersion((v) => v + 1);
      // Authoritative refresh: what the server committed is what we show.
      // Never trust the PATCH echo alone (stale cache / partial write).
      const fresh = await getAppSettings(apiBase);
      setEntries(fresh);
      setDraft({});
      setSecretMode("keep");
      setSecretDraft("");
      setFieldErrors({});
      push("Pengaturan tersimpan", "success");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Gagal menyimpan";
      // Toast AND banner: failures must be impossible to miss even if the
      // banner is below the fold.
      setSaveError(msg);
      push(`Gagal menyimpan: ${msg}`, "error");
      if (typeof console !== "undefined") console.error("[settings] save failed:", e);
    } finally {
      setSaving(false);
    }
  };

  const onCancel = () => {
    setDraft({});
    setFieldErrors({});
    setSecretMode("keep");
    setSecretDraft("");
    setSaveError(null);
  };

  const onResetField = async () => {
    if (!resetKey) return;
    setResetting(true);
    try {
      setEntries(await resetAppSetting(apiBase, resetKey));
      if (resetKey === "GEMINI_API_KEY") setApiKeyVersion((v) => v + 1);
      setDraft((d) => {
        const next = { ...d };
        delete next[resetKey];
        return next;
      });
      push(`${resetKey} dikembalikan ke bawaan`, "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Reset gagal", "error");
    } finally {
      setResetting(false);
      setResetKey(null);
    }
  };

  const ERROR_HINTS: Record<string, string> = {
    missing_api_key: "Belum ada API key tersimpan.",
    invalid_api_key: "API key salah / tidak valid.",
    authentication_error: "Autentikasi ditolak server Gemini.",
    model_not_found: "Nama model tidak tersedia.",
    permission_denied: "Akses ditolak untuk key/model ini.",
    timeout: "Batas waktu permintaan — coba naikkan Batas Waktu Gemini.",
    rate_limit: "Terkena batas permintaan — tunggu sebentar lalu coba lagi.",
    network_error: "Gangguan jaringan ke server Gemini.",
    client_lifecycle_error: "Lifecycle client bermasalah (bug internal).",
    invalid_response: "Respons tak terduga dari Gemini.",
  };

  const onTest = async () => {
    setTestMsg(null);
    setTesting(true);
    try {
      const key = secretMode === "replace" && secretDraft.trim() ? secretDraft.trim() : undefined;
      const res = await testGemini(apiBase, key);
      const ms = res.latency_ms != null ? ` (${res.latency_ms} ms)` : "";
      const disc = res.discovery;
      const discText =
        disc && disc.status !== "skipped" && disc.status === "ok" && disc.models != null
          ? ` Model ditemukan: ${disc.models}, kompatibel: ${disc.compatible ?? "—"}, free-tier: ${disc.free_tier ?? "—"}.`
          : "";
      setTestMsg({ ok: true, text: `Koneksi berhasil — ${res.model}${ms}.${discText}` });
    } catch (e) {
      const code = (e as { code?: string })?.code;
      const hint = (code && ERROR_HINTS[code]) || "";
      const base = e instanceof Error ? e.message : "Tes gagal";
      setTestMsg({ ok: false, text: hint ? `Koneksi gagal: ${hint} [${code}]` : `Koneksi gagal: ${base}` });
    } finally {
      setTesting(false);
    }
  };

  const saveBase = async (e: FormEvent) => {
    e.preventDefault();
    const v = baseInput.trim().replace(/\/+$/, "");
    if (!v) return;
    setTesting(true);
    try {
      const h = await checkHealth(v);
      if (!h.api) throw new Error("API tidak merespons di alamat itu");
      setApiBase(v);
      push("Endpoint API disimpan & terhubung", "success");
    } catch (err) {
      push(err instanceof Error ? err.message : "Koneksi gagal", "error");
    } finally {
      setTesting(false);
    }
  };

  const renderField = (e: AppSetting) => {
    const val = effectiveValue(e);
    if (e.type === "enum") {
      return (
        <select value={String(val)} onChange={(ev) => setDraftValue(e, ev.target.value)} aria-label={e.label}>
          {e.options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      );
    }
    if (e.type === "string") {
      return <input type="text" value={String(val)} onChange={(ev) => setDraftValue(e, ev.target.value)} aria-label={e.label} />;
    }
    return (
      <UnitField
        setting={e}
        value={Number(val)}
        onChange={(n) => setDraftValue(e, n)}
        error={fieldErrors[e.key] || null}
      />
    );
  };

  const fieldsByBackend = useMemo(() => {
    const map: Record<string, AppSetting[]> = {};
    for (const e of nonSecret) {
      (map[e.category] ??= []).push(e);
    }
    return map;
  }, [nonSecret]);

  const genericFieldProps = { effectiveValue, renderField, fieldErrors, onResetKey: setResetKey };

  const geminiAdvancedFields = useMemo(
    () => splitGeminiAdvanced(fieldsByBackend["AI & Models"] ?? []).advanced,
    [fieldsByBackend]
  );

  const searchHits = useMemo(
    () =>
      searchSettings(
        query,
        nonSecret.map((e) => ({ key: e.key, label: e.label, description: e.description, category: e.category })),
        (key) => (GEMINI_ADVANCED_KEYS.includes(key) ? "advanced" : undefined),
        (backend) => {
          const found = SETTINGS_CATEGORIES.find((c) => c.backendCategories.includes(backend));
          return found ? found.id : "advanced";
        }
      ),
    [query, nonSecret]
  );

  const jumpToHit = (hit: { category: SettingsCategoryId; geminiTab?: GeminiTabId }) => {
    setCategory(hit.category);
    if (hit.geminiTab) setGeminiTab(hit.geminiTab);
    setQuery("");
    setHitsOpen(false);
  };

  return (
    <div className="page">
      <PageHeader eyebrow="PENGATURAN" title="Pengaturan" desc="Perilaku aplikasi, koneksi, dan informasi sistem." />

      <div className="settings-search">
        <SearchInput
          value={query}
          onChange={(v) => {
            setQuery(v);
            setHitsOpen(true);
          }}
          onFocus={() => setHitsOpen(true)}
          onBlur={() => setTimeout(() => setHitsOpen(false), 120)}
          placeholder="Cari pengaturan… (mis. timeout, provider, fps)"
          ariaLabel="Cari pengaturan"
        />
        {hitsOpen && searchHits.length > 0 && (
          <div className="settings-hits" role="listbox" aria-label="Hasil pencarian">
            {searchHits.map((h, i) => (
              <button key={`${h.category}-${h.title}-${i}`} type="button" role="option" aria-selected="false" onMouseDown={() => jumpToHit(h)}>
                <strong>{h.title}</strong>
                <small>{h.detail}</small>
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="settings-layout">
        <CategoryNav
          active={category}
          onPick={(id) => {
            setCategory(id);
            setHitsOpen(false);
          }}
        />

        <div className="settings-content">
          {loading || !entries ? (
            <p className="muted">Memuat…</p>
          ) : (
            <>
              {category === "general" && (
                <>
                  <section className="card" aria-label="Koneksi API">
          <div className="card-head">
            <h2>Koneksi API</h2>
            <p>Endpoint backend yang dipakai semua halaman.</p>
          </div>
          <form onSubmit={saveBase} className="form">
            <div className="field">
              <label htmlFor="api-base">API base URL</label>
              <input id="api-base" type="url" required value={baseInput} onChange={(e) => setBaseInput(e.target.value)} placeholder="http://127.0.0.1:8000" />
            </div>
            <button type="submit" className="btn btn-filled btn-sm" disabled={testing}>
              {testing ? "Menguji…" : "Simpan & uji koneksi"}
            </button>
          </form>
          <ul className="kv">
            <li>
              <span>API</span>
              <span className={`dot dot-${health ? (health.api ? "ok" : "err") : "idle"}`}>
                {health ? (health.api ? "Terhubung" : "Terputus") : "…"}
              </span>
            </li>
            <li>
              <span>Worker</span>
              <span className={`dot dot-${health ? (health.worker ? "ok" : "err") : "idle"}`}>
                {health ? (health.worker ? "Siap" : "Bermasalah") : "…"}
              </span>
            </li>
          </ul>
        </section>

        <section className="card" aria-label="Preferensi">
          <div className="card-head">
            <h2>Preferensi</h2>
            <p>Tersimpan lokal di browser ini.</p>
          </div>
          <div className="form">
            <div className="field">
              <label htmlFor="theme">Tema</label>
              <select id="theme" value={theme} onChange={(e) => setTheme(e.target.value as typeof theme)}>
                <option value="system">Ikuti sistem</option>
                <option value="light">Terang</option>
                <option value="dark">Gelap</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="poll">Interval penyegaran</label>
              <select id="poll" value={pollMs} onChange={(e) => setPollMs(Number(e.target.value))}>
                <option value={0}>Mati (manual)</option>
                <option value={2000}>Tiap 2 detik</option>
                <option value={5000}>Tiap 5 detik</option>
                <option value={10000}>Tiap 10 detik</option>
              </select>
            </div>
          </div>
        </section>
              </>
            )}

            {category === "ai" && (
              <>
                <GeminiSection
                  apiBase={apiBase}
                  apiKeyVersion={apiKeyVersion}
                  push={push}
                  tab={geminiTab}
                  setTab={setGeminiTab}
                  advancedFields={geminiAdvancedFields}
                  genericFieldProps={genericFieldProps}
                />

                <section className="card" aria-label="Auto Framing Intelligence">
                  <div className="card-head">
                    <h2>Auto Framing Intelligence</h2>
                    <p>Deteksi speaker lanjutan (TalkNet) untuk kasus ambigu. Heuristik tetap jalan dulu.</p>
                  </div>
                  <ul className="kv">
                    <li>
                      <span>Status</span>
                      <span className="mono-sm">
                        {talknetStatus == null
                          ? "…"
                          : !talknetStatus.enabled
                            ? "○ Nonaktif"
                            : talknetStatus.status === "READY"
                              ? "● Siap"
                              : talknetStatus.status === "STANDBY"
                                ? "● Siaga (model ada, dimuat saat eskalasi)"
                                : talknetStatus.status === "MODEL_MISSING"
                                  ? "● Model hilang"
                                  : `● ${talknetStatus.status}`}
                      </span>
                    </li>
                    {talknetStatus?.enabled && (
                      <>
                        <li>
                          <span>Perangkat</span>
                          <span className="mono-sm">
                            {talknetStatus.worker?.device
                              ? `${talknetStatus.worker.device} (worker)`
                              : (talknetStatus.device_name ?? talknetStatus.device ?? "—")}
                            {!talknetStatus.worker?.device && talknetStatus.backend && talknetStatus.backend !== "cpu"
                              ? ` (${talknetStatus.backend})`
                              : ""}
                          </span>
                        </li>
                        <li>
                          <span>Mode resource</span>
                          <span className="mono-sm">{talknetStatus.resource_mode ?? "—"}</span>
                        </li>
                      </>
                    )}
                  </ul>
                  <GenericFields
                    fields={talknetFields}
                    effectiveValue={effectiveValue}
                    renderField={renderField}
                    fieldErrors={fieldErrors}
                    onResetKey={setResetKey}
                  />
                  <p className="muted">
                    <small>Ambang eskalasi, bobot fusi, cache, dan model path via variabel TALKNET_* (lihat dokumentasi).</small>
                  </p>
                </section>

                {(secretEntry || legacyFields.length > 0) && (
                  <Collapsible
                    title="Kompatibilitas Legacy"
                    subtitle="Konfigurasi Gemini lama — gunakan Provider untuk yang baru"
                    defaultOpen={false}
                    badge={secretEntry?.configured ? (
                      <span className="chip chip-ok">Key lama aktif</span>
                    ) : undefined}
                  >
                    <p className="muted">
                      <small>
                        Dipertahankan untuk kompatibilitas instalasi/job lama. Runtime memakai
                        Provider Gemini bila tersedia dan hanya memakai konfigurasi ini bila
                        Providers kosong. Jangan gunakan untuk konfigurasi baru.
                      </small>
                    </p>
                    {secretEntry && (
                    <section className="card" aria-label="API key Gemini lama">
                    <div className="card-head">
                      <h2>API Key Lama</h2>
                      <p>Key tunggal lama — dipakai bila Providers kosong. Sebaiknya migrasi ke Providers.</p>
                    </div>
                    <div className="secret-box">
                <div className="secret-head">
                  <div>
                    <strong>Gemini API Key</strong>
                      <span className={`chip chip-${secretEntry.configured ? "ok" : "muted"}`}>
                        {secretEntry.configured ? "Terkonfigurasi" : "Belum dikonfigurasi"}
                      </span>
                    </div>
                    <div className="secret-actions">
                      {secretEntry.configured && secretMode === "keep" && (
                        <>
                          <button type="button" className="btn btn-tonal btn-sm" onClick={() => setSecretMode("replace")}>
                            Ganti
                          </button>
                          <button type="button" className="btn btn-text btn-sm" onClick={() => setSecretMode("remove")}>
                            Hapus
                          </button>
                        </>
                      )}
                      {!secretEntry.configured && secretMode === "keep" && (
                        <button type="button" className="btn btn-tonal btn-sm" onClick={() => setSecretMode("replace")}>
                          Isi key
                        </button>
                      )}
                      {secretMode !== "keep" && (
                        <button type="button" className="btn btn-text btn-sm" onClick={() => { setSecretMode("keep"); setSecretDraft(""); }}>
                          Batal
                        </button>
                      )}
                      <button type="button" className="btn btn-tonal btn-sm" onClick={onTest} disabled={testing}>
                        {testing ? "Menguji…" : testMsg?.ok ? "Uji Lagi" : "Uji Koneksi"}
                      </button>
                  </div>
                </div>
                <div className="secret-value mono-sm" aria-label="Status API key">
                  {secretMode === "remove" ? "Akan dihapus saat disimpan." : secretMode === "replace" ? "••••••••••••••••" : secretEntry.configured ? "••••••••••••••••" : "Belum ada key."}
                </div>
                {secretMode === "replace" && (
                  <input
                    type="password"
                    value={secretDraft}
                    onChange={(e) => setSecretDraft(e.target.value)}
                    placeholder="Tempel API key baru…"
                    aria-label="API key baru"
                    autoComplete="off"
                  />
                )}
                {secretMode === "remove" && (
                  <p className="muted">Key akan dihapus dari penyimpanan aman saat kamu menekan Simpan Perubahan.</p>
                )}
                <p className="muted">
                  Tes memakai konfigurasi tersimpan
                  {secretMode === "replace" && secretDraft.trim()
                    ? " (atau key baru yang sedang diketik — tanpa menyimpan)"
                    : ""}
                  .
                </p>
                {testMsg && (
                  <p className={`banner ${testMsg.ok ? "banner-ok" : "banner-error"}`} role="status">
                    {testMsg.text}
                  </p>
                )}
                  </div>
                  </section>
                )}
                {legacyFields.length > 0 && (
                  <GenericFields
                    fields={legacyFields}
                    effectiveValue={effectiveValue}
                    renderField={renderField}
                    fieldErrors={fieldErrors}
                    onResetKey={setResetKey}
                  />
                )}
                  </Collapsible>
                )}
              </>
            )}
            {category === "video" && (
              <section className="card" aria-label="Video dan Rendering">
                <div className="card-head">
                  <h2>Video & Rendering</h2>
                  <p>Kualitas render, resolusi, dan framing output. Berlaku untuk job baru.</p>
                </div>
                <GenericFields
                  fields={fieldsByBackend["Processing"] ?? []}
                  effectiveValue={effectiveValue}
                  renderField={renderField}
                  fieldErrors={fieldErrors}
                  onResetKey={setResetKey}
                />
              </section>
            )}

            {category === "transcript" && (
              <section className="card" aria-label="Transkripsi">
                <div className="card-head">
                  <h2>Transkripsi</h2>
                  <p>Bahasa, subtitle, dan pemrosesan transkrip. Berlaku untuk job baru.</p>
                </div>
                <GenericFields
                  fields={fieldsByBackend["Transcript & LLM"] ?? []}
                  effectiveValue={effectiveValue}
                  renderField={renderField}
                  fieldErrors={fieldErrors}
                  onResetKey={setResetKey}
                />
              </section>
            )}

            {(category === "transcript" || category === "advanced") && (
              <YoutubeCookiesCard apiBase={apiBase} push={push} />
            )}

            {category === "advanced" && (
              <>
                <section className="card" aria-label="Penyimpanan dan retensi">
                  <div className="card-head">
                    <h2>Penyimpanan & Retensi</h2>
                    <p>Ukuran artifact dan masa simpan file.</p>
                  </div>
                  <GenericFields
                    fields={fieldsByBackend["Storage & Retention"] ?? []}
                    effectiveValue={effectiveValue}
                    renderField={renderField}
                    fieldErrors={fieldErrors}
                    onResetKey={setResetKey}
                  />
                </section>
                <section className="card" aria-label="Logging">
                  <div className="card-head">
                    <h2>Logging</h2>
                    <p>Verbosity log aplikasi.</p>
                  </div>
                  <GenericFields
                    fields={fieldsByBackend["Logging"] ?? []}
                    effectiveValue={effectiveValue}
                    renderField={renderField}
                    fieldErrors={fieldErrors}
                    onResetKey={setResetKey}
                  />
                </section>
              </>
            )}

            {saveError && (
              <p className="banner banner-error" role="alert">
                {saveError}
              </p>
            )}
            </>
          )}
        </div>
      </div>

      {category === "system" && (
      <section className="card" aria-label="Informasi sistem">
        <div className="card-head">
          <h2>Informasi Sistem</h2>
          <p>Hanya baca. Diubah lewat deployment, bukan dari UI.</p>
        </div>
        {!config?.system ? (
          <p className="muted">Memuat…</p>
        ) : (
          <ul className="kv">
            <li>
              <span>API</span>
              <span className="mono-sm">
                {config.system.api_host}:{config.system.api_port} ·{" "}
                <span className={`dot dot-${health?.api ? "ok" : "err"}`}>{health?.api ? "Terhubung" : "Terputus"}</span>
              </span>
            </li>
            <li>
              <span>Runtime</span>
              <span className="mono-sm">
                {config.runtime === "desktop"
                  ? `Desktop (SQLite + lokal${config.runner ? ` · ${config.runner}` : ""})`
                  : `Server${config.runner ? ` · ${config.runner}` : ""}`}
              </span>
            </li>
            {config.runtime === "desktop" ? (
              <li>
                <span>Penyimpanan</span>
                <span className="mono-sm">Lokal (SQLite, tanpa Redis/Celery)</span>
              </li>
            ) : (
              <li>
                <span>Redis</span>
                <span className={`dot dot-${config.system.redis_connected ? "ok" : "err"}`}>
                  {config.system.redis_connected ? "Terhubung" : "Terputus"}
                </span>
              </li>
            )}
            <li>
              <span>Storage</span>
              <span className="mono-sm">
                {config.system.storage_root}
                {config.system.storage_free_bytes != null && (
                  <> · {formatBytes(config.system.storage_free_bytes)} tersisa</>
                )}
              </span>
            </li>
            <li>
              <span>Origin frontend</span>
              <span className="mono-sm">{config.system.frontend_origins.join(", ")}</span>
            </li>
            <li>
              <span>Mode publik</span>
              <span className="mono-sm">{config.system.public_mode ? "Aktif" : "Nonaktif"}</span>
            </li>
          </ul>
        )}
      </section>
      )}

      {dirty && (
        <div className="savebar" role="region" aria-label="Simpan perubahan">
          <span className="chip chip-warn">Ada perubahan</span>
          <div className="savebar-actions">
            <button type="button" className="btn btn-text" onClick={onCancel} disabled={saving}>
              Batal
            </button>
            <button type="button" className="btn btn-filled" onClick={onSave} disabled={saving || hasErrors}>
              {saving ? "Menyimpan…" : "Simpan Perubahan"}
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={resetKey !== null}
        title="Kembalikan ke default?"
        message={resetKey ? `Kembalikan ${resetKey} ke nilai default?` : ""}
        confirmLabel="Ya, kembalikan"
        busy={resetting}
        onConfirm={onResetField}
        onClose={() => !resetting && setResetKey(null)}
      />
    </div>
  );
}
