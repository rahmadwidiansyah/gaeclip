"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Collapsible, Icon } from "../components/ui";
import { getAiModels, getAiSelection, patchAppSettings, refreshAiModels, formatTime } from "../lib/api";
import { channelBadge, filterModels, pricingBadge, selectableModels, sortModels, type ModelFacet } from "../lib/ai-models";
import type { AiModelInfo, AiSelection } from "../lib/types";

const MAX_FALLBACKS = 5;

const FACETS: { id: ModelFacet; label: string }[] = [
  { id: "all", label: "Semua" },
  { id: "free", label: "Gratis" },
  { id: "stable", label: "Stabil" },
  { id: "preview", label: "Pratinjau" },
  { id: "flash", label: "Flash" },
  { id: "pro", label: "Pro" },
  { id: "lite", label: "Lite" },
];

function ModelBadges({ model }: { model: AiModelInfo }) {
  return (
    <span className="chip-row">
      <span className={`mini-chip${model.release_channel === "stable" ? " mini-chip-done" : ""}`}>
        {channelBadge(model.release_channel)}
      </span>
      <span className="mini-chip">{model.variant}</span>
      <span className={`mini-chip${model.free_tier === "free" ? " mini-chip-done" : ""}`}>
        {pricingBadge(model.free_tier)}
      </span>
      {!model.eligible_for_gaeclip && <span className="mini-chip mini-chip-failed">TIDAK DIDUKUNG</span>}
      {!model.available && <span className="mini-chip mini-chip-failed">TIDAK TERSEDIA</span>}
    </span>
  );
}

export default function AiModelsSection({
  apiBase,
  apiKeyVersion,
  push,
}: {
  apiBase: string;
  apiKeyVersion: number;
  push: (message: string, kind?: "info" | "success" | "error") => void;
}) {
  const [models, setModels] = useState<AiModelInfo[] | null>(null);
  const [source, setSource] = useState<string>("");
  const [counts, setCounts] = useState<{ discovered: number; compatible: number; free_tier: number; unsupported: number } | null>(null);
  const [discoveredAt, setDiscoveredAt] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selection, setSelection] = useState<AiSelection | null>(null);
  const [mode, setMode] = useState("automatic");
  const [primary, setPrimary] = useState("");
  const [fallbacks, setFallbacks] = useState<string[]>([]);
  const [freeTierOnly, setFreeTierOnly] = useState(true);
  const [serverPrefs, setServerPrefs] = useState<{ mode: string; primary: string; fallbacks: string[]; free: boolean } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [q, setQ] = useState("");
  const [facet, setFacet] = useState<ModelFacet>("all");

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const res = await getAiModels(apiBase);
      setModels(res.models);
      setSource(res.source);
      setCounts(res.counts);
      setDiscoveredAt(res.discovered_at);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "Gagal memuat daftar model");
      setModels(null);
    } finally {
      setLoading(false);
    }
    try {
      const sel = await getAiSelection(apiBase);
      setSelection(sel.selection);
      setMode(sel.prefs.mode);
      setPrimary(sel.prefs.primary_model);
      setFallbacks(sel.prefs.fallback_models ?? []);
      setFreeTierOnly(sel.prefs.free_tier_only);
      setServerPrefs({
        mode: sel.prefs.mode,
        primary: sel.prefs.primary_model,
        fallbacks: sel.prefs.fallback_models ?? [],
        free: sel.prefs.free_tier_only,
      });
    } catch {
      /* prefs stay at defaults; save will surface errors */
    }
  }, [apiBase]);

  useEffect(() => {
    load();
  }, [load, apiKeyVersion]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      const res = await refreshAiModels(apiBase);
      setModels(res.models);
      setSource(res.source);
      setCounts({ discovered: res.discovered, compatible: res.compatible, free_tier: res.free_tier, unsupported: res.unsupported });
      setDiscoveredAt(res.discovered_at);
      setSelection(res.selection);
      setLoadError(null);
      push(`Discovery: ${res.discovered} model, ${res.compatible} kompatibel`, "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Refresh gagal", "error");
    } finally {
      setRefreshing(false);
    }
  };

  const options = useMemo(
    () => (models ? selectableModels(models, freeTierOnly) : []),
    [models, freeTierOnly]
  );
  const browsable = useMemo(
    () => (models ? filterModels(sortModels(models), q, facet) : []),
    [models, q, facet]
  );
  const chosen = useMemo(() => new Set([primary, ...fallbacks].filter(Boolean)), [primary, fallbacks]);
  // Current primary kept visible even when it left the registry (Q).
  const primaryMissing = primary !== "" && models !== null && !models.some((m) => m.id === primary);

  const dirty =
    serverPrefs === null ||
    mode !== serverPrefs.mode ||
    primary !== serverPrefs.primary ||
    freeTierOnly !== serverPrefs.free ||
    JSON.stringify(fallbacks) !== JSON.stringify(serverPrefs.fallbacks);

  const onSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await patchAppSettings(apiBase, {
        AI_MODEL_MODE: mode,
        AI_PRIMARY_MODEL: primary,
        AI_FALLBACK_MODELS: JSON.stringify(fallbacks),
        AI_FREE_TIER_ONLY: freeTierOnly,
      });
      const sel = await getAiSelection(apiBase);
      setSelection(sel.selection);
      setServerPrefs({
        mode: sel.prefs.mode,
        primary: sel.prefs.primary_model,
        fallbacks: sel.prefs.fallback_models ?? [],
        free: sel.prefs.free_tier_only,
      });
      push("Konfigurasi model AI tersimpan", "success");
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Gagal menyimpan";
      setSaveError(msg);
      push(`Gagal menyimpan: ${msg}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const moveFallback = (index: number, dir: -1 | 1) => {
    setFallbacks((fb) => {
      const next = [...fb];
      const j = index + dir;
      if (j < 0 || j >= next.length) return next;
      [next[index], next[j]] = [next[j], next[index]];
      return next;
    });
  };

  const addFallback = () => {
    const candidate = options.find((m) => !chosen.has(m.id));
    if (candidate && fallbacks.length < MAX_FALLBACKS) setFallbacks((fb) => [...fb, candidate.id]);
  };

  return (
    <section className="card" aria-label="Model AI Gemini">
      <div className="card-head row">
        <div>
          <h2>Model AI Gemini</h2>
          <p>Discovery backend · rantai primary + fallback · tanpa model hardcoded.</p>
        </div>
        <button type="button" className="btn btn-tonal btn-sm" onClick={onRefresh} disabled={refreshing || loading}>
          <Icon name="refresh" size={15} /> {refreshing ? "Merefresh…" : "Refresh Model"}
        </button>
      </div>

      {loading ? (
        <p className="muted">Memuat daftar model…</p>
      ) : loadError || !models ? (
        <div className="banner banner-error" role="alert">
          <Icon name="alert" size={16} />
          <span>{loadError ?? "Daftar model tidak tersedia."} Konfigurasi tersimpan tetap berlaku; coba Refresh Model.</span>
        </div>
      ) : (
        <>
          <ul className="kv">
            <li>
              <span>Models tersedia</span>
              <span>
                {counts?.compatible ?? 0} kompatibel · {counts?.free_tier ?? 0} free-tier · {counts?.discovered ?? 0} ditemukan
              </span>
            </li>
            <li>
              <span>Terakhir dicek</span>
              <span>
                {discoveredAt ? formatTime(discoveredAt) : "—"}
                {source === "cache" && <span className="mini-chip">CACHE</span>}
                {source === "cache-stale" && <span className="mini-chip mini-chip-failed">CACHE · BASI</span>}
              </span>
            </li>
            {selection?.method && (
              <li>
                <span>Rantai aktif</span>
                <span className="mono-sm">
                  {selection.method} · {selection.primary ?? "—"}
                  {selection.fallbacks.length > 0 && ` → ${selection.fallbacks.join(" → ")}`}
                </span>
              </li>
            )}
          </ul>

          {primaryMissing && (
            <div className="banner banner-error" role="alert">
              <Icon name="alert" size={16} />
              <span>Model saat ini sudah tidak tersedia: {primary}. Pilih model lain di bawah.</span>
            </div>
          )}

          <h3 className="subhead">Pemilihan Model</h3>
          <div className="set-grid">
            <div className="set-field">
              <div className="set-label"><label>Mode</label></div>
              <div className="radio-row" role="radiogroup" aria-label="Mode pemilihan model">
                {(["automatic", "manual"] as const).map((m) => (
                  <label key={m} className="check">
                    <input type="radio" name="ai-mode" checked={mode === m} onChange={() => setMode(m)} />
                    <span>{m === "automatic" ? "Otomatis" : "Manual"}</span>
                  </label>
                ))}
              </div>
              <small className="muted">
                {mode === "automatic"
                  ? "Sistem memilih model utama + fallback dari policy (stable, flash, free-tier)."
                  : "Kamu menentukan model utama + urutan fallback sendiri."}
              </small>
            </div>
            <div className="set-field">
              <div className="set-label"><label>Hanya Free Tier</label></div>
              <label className="check" htmlFor="ai-free">
                <input id="ai-free" type="checkbox" checked={freeTierOnly} onChange={(e) => setFreeTierOnly(e.target.checked)} />
                <span>{freeTierOnly ? "Hanya model FREE yang bisa dipilih" : "Semua model bisa dipilih"}</span>
              </label>
            </div>
          </div>

          <h3 className="subhead">Model Utama</h3>
          <div className="set-field">
            <select
              value={primary}
              onChange={(e) => setPrimary(e.target.value)}
              aria-label="Model utama"
              disabled={mode !== "manual" && options.length === 0}
            >
              <option value="">{mode === "automatic" ? "Otomatis (ditentukan policy)" : "Pilih model utama…"}</option>
              {primaryMissing && <option value={primary}>{primary} (Tidak tersedia)</option>}
              {options.filter((m) => m.id === primary || !chosen.has(m.id) || m.id === primary).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.display_name} · {m.release_channel} · {m.free_tier}
                </option>
              ))}
            </select>
          </div>

          <h3 className="subhead">Model Fallback</h3>
          {fallbacks.length === 0 ? (
            <p className="muted">{mode === "automatic" ? "Fallback dibuat otomatis oleh policy." : "Belum ada fallback."}</p>
          ) : (
            <div className="fb-list">
              {fallbacks.map((fb, i) => (
                <div key={`${fb}-${i}`} className="fb-row">
                  <span className="mono-sm muted">{i + 1}.</span>
                  <select value={fb} onChange={(e) => setFallbacks((cur) => cur.map((v, j) => (j === i ? e.target.value : v)))} aria-label={`Fallback ${i + 1}`}>
                    {options
                      .filter((m) => m.id === fb || (!chosen.has(m.id) && m.id !== primary))
                      .map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.display_name} · {m.release_channel} · {m.free_tier}
                        </option>
                      ))}
                  </select>
                  <button type="button" className="btn btn-icon btn-sm" onClick={() => moveFallback(i, -1)} disabled={i === 0} aria-label={`Naikkan fallback ${i + 1}`}>
                    <Icon name="chevU" size={14} />
                  </button>
                  <button type="button" className="btn btn-icon btn-sm" onClick={() => moveFallback(i, 1)} disabled={i === fallbacks.length - 1} aria-label={`Turunkan fallback ${i + 1}`}>
                    <Icon name="chevD" size={14} />
                  </button>
                  <button type="button" className="btn btn-icon btn-sm btn-danger-ghost" onClick={() => setFallbacks((cur) => cur.filter((_, j) => j !== i))} aria-label={`Hapus fallback ${i + 1}`}>
                    <Icon name="close" size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="fb-actions">
            <button type="button" className="btn btn-tonal btn-sm" onClick={addFallback} disabled={fallbacks.length >= MAX_FALLBACKS || !options.some((m) => !chosen.has(m.id))}>
              <Icon name="plus" size={14} /> Tambah fallback
            </button>
          </div>

          <h3 className="subhead">Model yang Tersedia</h3>
          <Collapsible
            title="Jelajahi model"
            subtitle={`${browsable.length} ditampilkan · cari & filter di dalam`}
            defaultOpen={false}
          >
          <div className="toolbar">
            <div className="search">
              <Icon name="search" size={16} />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Cari model…" aria-label="Cari model" />
            </div>
            <select value={facet} onChange={(e) => setFacet(e.target.value as ModelFacet)} aria-label="Filter model">
              {FACETS.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.label}
                </option>
              ))}
            </select>
          </div>
          <ul className="mini-list model-list">
            {browsable.slice(0, 30).map((m) => (
              <li key={m.id} className="model-row">
                <span className="link-row-main">
                  <strong className="ellipsis" title={m.id}>{m.display_name}</strong>
                  <ModelBadges model={m} />
                </span>
                <span className="link-row-side mono-sm">{m.generation || m.version || "—"}</span>
              </li>
            ))}
          </ul>
          {browsable.length > 30 && <p className="muted">+ {browsable.length - 30} model lain (gunakan pencarian/filter).</p>}
          </Collapsible>

          {saveError && (
            <p className="banner banner-error" role="alert">
              {saveError}
            </p>
          )}
          <div className="set-foot">
            <small className="muted">Validasi atomik di backend: duplikat model utama/fallback dinormalisasi, yang tidak memenuhi syarat ditolak.</small>
            <button type="button" className="btn btn-filled btn-sm" onClick={onSave} disabled={saving || !dirty}>
              {saving ? "Menyimpan…" : "Simpan Perubahan"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}
