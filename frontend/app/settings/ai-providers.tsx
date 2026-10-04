"use client";

import { useCallback, useEffect, useState } from "react";
import { Collapsible, Icon } from "../components/ui";
import {
  addProvider,
  deleteProvider,
  editProvider,
  getProviderModels,
  getProvidersHealth,
  listProviders,
  refreshProviderModels,
  reorderProviders,
  resetProviderHealth,
  testProvider,
  testProviderModel,
} from "../lib/api";
import { accordionOpenId, cooldownText, healthBadge, moveId, timeAgo, QUOTA_NOTE } from "../lib/ai-providers";
import { selectableModels } from "../lib/ai-models";
import type { GeminiProviderInfo, ProviderDiscoveryModel } from "../lib/types";

function HealthChip({ state, cooldownUntil }: { state?: string; cooldownUntil?: number }) {
  const badge = healthBadge(state);
  const cd = cooldownText(cooldownUntil);
  return (
    <span className="chip-row">
      <span className={`mini-chip ${badge.tone}`}>● {badge.label}</span>
      {cd && <span className="mini-chip">{cd}</span>}
    </span>
  );
}

export default function AiProvidersSection({
  apiBase,
  push,
}: {
  apiBase: string;
  push: (message: string, kind?: "info" | "success" | "error") => void;
}) {
  const [providers, setProviders] = useState<GeminiProviderInfo[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [discovery, setDiscovery] = useState<Record<string, ProviderDiscoveryModel[]>>({});
  const [newName, setNewName] = useState("");
  const [newKey, setNewKey] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [modelDraft, setModelDraft] = useState<Record<string, string>>({});
  const [testModel, setTestModel] = useState<Record<string, string>>({});
  // Accordion by stable provider id: SEMUA COLLAPSED by default, maksimal
  // satu expanded. UI state saja (tidak disimpan ke database).
  const [openId, setOpenId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await listProviders(apiBase);
      const list: GeminiProviderInfo[] = res.providers ?? [];
      setProviders(list);
      setOpenId((prev) => (prev && list.some((p) => p.id === prev) ? prev : null));
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal memuat provider", "error");
      setProviders(null);
    } finally {
      setLoading(false);
    }
  }, [apiBase, push]);

  useEffect(() => {
    load();
  }, [load]);

  const reloadHealth = useCallback(async () => {
    try {
      const res = await getProvidersHealth(apiBase);
      if (res.providers) setProviders(res.providers);
    } catch {
      /* health is best-effort */
    }
  }, [apiBase]);

  async function wrap(key: string, fn: () => Promise<unknown>, ok?: string) {
    setBusy(key);
    try {
      await fn();
      await load();
      if (ok) push(ok, "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Operasi gagal", "error");
    } finally {
      setBusy(null);
    }
  }

  const onAdd = () =>
    wrap("add", async () => {
      if (!newName.trim() || !newKey.trim()) throw new Error("Nama dan API key wajib diisi");
      await addProvider(apiBase, { name: newName.trim(), api_key: newKey.trim(), models: [] });
      setNewName("");
      setNewKey("");
      setShowAdd(false);
    }, "Provider ditambahkan. Jalankan Temukan Model untuk mengisi daftar model.");

  const onDiscover = (p: GeminiProviderInfo) =>
    wrap(`discover-${p.id}`, async () => {
      const res = await getProviderModels(apiBase, p.id);
      setDiscovery((d) => ({ ...d, [p.id]: res.models ?? [] }));
    });

  const onRefreshModels = (p: GeminiProviderInfo) =>
    wrap(`refresh-${p.id}`, async () => {
      const res = await refreshProviderModels(apiBase, p.id);
      const removed: string[] = res.removed_marked_unavailable ?? [];
      push(
        removed.length > 0
          ? `Discovery: ${res.discovered ?? 0} model, ${removed.length} ditandai tidak tersedia`
          : `Discovery: ${res.discovered ?? 0} model`,
        "success"
      );
      await onDiscoverSilent(p.id);
    });

  const onDiscoverSilent = async (id: string) => {
    try {
      const res = await getProviderModels(apiBase, id);
      setDiscovery((d) => ({ ...d, [id]: res.models ?? [] }));
    } catch {
      /* silent */
    }
  };

  const onTest = (p: GeminiProviderInfo) =>
    wrap(`test-${p.id}`, async () => {
      const res = await testProvider(apiBase, p.id);
      push(`Koneksi OK (${res.model ?? "model"}, ${res.latency_ms ?? "?"}ms)`, "success");
      await reloadHealth();
    });

  const onTestModel = (p: GeminiProviderInfo) =>
    wrap(`testmodel-${p.id}`, async () => {
      const model = (testModel[p.id] || p.models.find((m) => m.enabled)?.id || "").trim();
      if (!model) throw new Error("Pilih model dulu");
      const res = await testProviderModel(apiBase, p.id, model);
      push(`Model OK (${res.model}, ${res.latency_ms ?? "?"}ms)`, "success");
    });

  const moveProvider = (index: number, dir: -1 | 1) => {
    if (!providers) return;
    const ids = moveId(providers.map((p) => p.id), index, dir);
    if (JSON.stringify(ids) === JSON.stringify(providers.map((p) => p.id))) return;
    void wrap("reorder", () => reorderProviders(apiBase, ids));
  };

  const onSetMode = (p: GeminiProviderInfo, model_mode: string) =>
    wrap(`mode-${p.id}`, () => editProvider(apiBase, p.id, { model_mode }), `Sumber model: ${model_mode}`);

  const onSetDepth = (p: GeminiProviderInfo, auto_depth: number) => {
    if (!Number.isInteger(auto_depth) || auto_depth < 1 || auto_depth > 10) return;
    wrap(`mode-${p.id}`, () => editProvider(apiBase, p.id, { auto_depth }));
  };

  const onSetFreeTier = (p: GeminiProviderInfo, auto_free_tier_only: boolean) =>
    wrap(`mode-${p.id}`, () => editProvider(apiBase, p.id, { auto_free_tier_only }));

  const moveModel = (p: GeminiProviderInfo, index: number, dir: -1 | 1) => {
    const ids = moveId(p.models.map((m) => m.id), index, dir);
    const models = ids.map((id, i) => {
      const m = p.models.find((x) => x.id === id)!;
      return { id: m.id, enabled: m.enabled, priority: i + 1 };
    });
    void wrap(`models-${p.id}`, () => editProvider(apiBase, p.id, { models }));
  };

  const toggleModel = (p: GeminiProviderInfo, id: string) => {
    const models = p.models.map((m, i) => (m.id === id ? { id: m.id, enabled: !m.enabled, priority: i + 1 } : { id: m.id, enabled: m.enabled, priority: i + 1 }));
    void wrap(`models-${p.id}`, () => editProvider(apiBase, p.id, { models }));
  };

  const removeModel = (p: GeminiProviderInfo, id: string) => {
    const models = p.models.filter((m) => m.id !== id).map((m, i) => ({ id: m.id, enabled: m.enabled, priority: i + 1 }));
    void wrap(`models-${p.id}`, () => editProvider(apiBase, p.id, { models }));
  };

  const addModel = (p: GeminiProviderInfo) => {
    const id = (modelDraft[p.id] || "").trim();
    if (!id) return;
    if (p.models.some((m) => m.id === id)) {
      push("Model sudah ada di provider ini", "error");
      return;
    }
    const models = [...p.models.map((m, i) => ({ id: m.id, enabled: m.enabled, priority: i + 1 })), { id, enabled: true, priority: p.models.length + 1 }];
    setModelDraft((d) => ({ ...d, [p.id]: "" }));
    void wrap(`models-${p.id}`, () => editProvider(apiBase, p.id, { models }), "Model ditambahkan");
  };

  const adoptDiscovered = (p: GeminiProviderInfo, id: string) => {
    if (p.models.some((m) => m.id === id)) return;
    const models = [...p.models.map((m, i) => ({ id: m.id, enabled: m.enabled, priority: i + 1 })), { id, enabled: true, priority: p.models.length + 1 }];
    void wrap(`models-${p.id}`, () => editProvider(apiBase, p.id, { models }), `Model ${id} ditambahkan`);
  };

  return (
    <section className="card" aria-label="Provider Gemini">
      <div className="card-head row">
        <div>
          <h2>Provider Gemini</h2>
          <p>Multi API key × multi model fallback. Key tidak pernah tampil utuh.</p>
        </div>
        <div className="row" style={{ gap: 8 }}>
          <button type="button" className="btn btn-text btn-sm" onClick={() => void wrap("reload", () => reloadHealth())} disabled={busy !== null} title="Segarkan status kesehatan">
            <Icon name="refresh" size={15} /> Segarkan
          </button>
          <button type="button" className="btn btn-tonal btn-sm" onClick={() => setShowAdd(true)} disabled={busy !== null}>
            <Icon name="plus" size={14} /> Tambah
          </button>
        </div>
      </div>
      <p className="muted"><small>{QUOTA_NOTE}</small></p>

      {loading ? (
        <p className="muted">Memuat provider…</p>
      ) : !providers ? (
        <p className="muted">Daftar provider tidak tersedia.</p>
      ) : providers.length === 0 ? (
        <p className="muted">Belum ada provider. Tambahkan API key untuk mengaktifkan kurasi AI.</p>
      ) : (
        <div className="prov-list">
          {providers.map((p, pi) => {
            const discovered = discovery[p.id] ?? [];
            const open = openId === p.id;
            const enabledModels = p.models.filter((m) => m.enabled).length;
            const lastOk = timeAgo(p.health?.last_success);
            return (
              <div key={p.id} className={`prov-card${open ? " open" : ""}${p.enabled ? "" : " off"}`}>
                <div className="prov-head" role="button" tabIndex={0}
                  aria-expanded={open}
                  title={open ? "Tutup detail" : "Buka detail"}
                  onClick={() => setOpenId((prev) => accordionOpenId(prev, p.id))}
                  onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpenId((prev) => accordionOpenId(prev, p.id)); } }}
                >
                  <span className="prov-rank" aria-hidden="true">{p.priority}</span>
                  <span className="prov-main">
                    <strong className="ellipsis" title={p.name}>{p.name}</strong>
                    <small className="muted ellipsis">
                      {p.models.length} model{enabledModels !== p.models.length ? ` (${enabledModels} aktif)` : ""} · Prioritas {p.priority}
                      {lastOk ? ` · Aktif ${lastOk}` : ""}
                    </small>
                  </span>
                  <HealthChip state={p.enabled ? p.health?.state : "DISABLED"} cooldownUntil={p.health?.cooldown_until} />
                  <label className="check" onClick={(e) => e.stopPropagation()} title={p.enabled ? "Nonaktifkan provider" : "Aktifkan provider"}>
                    <input
                      type="checkbox"
                      checked={p.enabled}
                      disabled={busy !== null}
                      onChange={(e) => void wrap(`toggle-${p.id}`, () => editProvider(apiBase, p.id, { enabled: e.target.checked }))}
                      aria-label={`Aktifkan ${p.name}`}
                    />
                  </label>
                  <span className={`coll-chev${open ? " open" : ""}`} aria-hidden="true">
                    <Icon name="chevD" size={16} />
                  </span>
                </div>

                {open && (
                  <div className="prov-body">
                    {(p.health?.last_error || !p.key_configured) && (
                      <p className="muted prov-err">
                        <small>
                          {!p.key_configured
                            ? "API key belum diisi."
                            : `Terakhir gagal: ${p.health?.last_error_category ?? "error"} — ${(p.health?.last_error ?? "").slice(0, 80)}`}
                        </small>
                      </p>
                    )}
                    <ul className="kv">
                      <li>
                        <span>API Key</span>
                        <span className="mono-sm">{p.key_configured ? `••••${p.key_suffix}` : <em>belum diisi</em>}</span>
                      </li>
                      <li>
                        <span>Prioritas</span>
                        <span className="row" style={{ gap: 4 }}>
                          <button type="button" className="btn btn-icon btn-sm" disabled={busy !== null || pi === 0} onClick={() => moveProvider(pi, -1)} aria-label="Naikkan prioritas provider" title="Naikkan prioritas"><Icon name="chevU" size={14} /></button>
                          <button type="button" className="btn btn-icon btn-sm" disabled={busy !== null || pi === providers.length - 1} onClick={() => moveProvider(pi, 1)} aria-label="Turunkan prioritas provider" title="Turunkan prioritas"><Icon name="chevD" size={14} /></button>
                        </span>
                      </li>
                      {typeof p.success_rate === "number" && (
                        <li>
                          <span>Keberhasilan</span>
                          <span className="mono-sm">{(p.success_rate * 100).toFixed(0)}%</span>
                        </li>
                      )}
                    </ul>

                    <div className="prov-actions">
                      <button type="button" className="btn btn-tonal btn-sm" disabled={busy !== null} onClick={() => onTest(p)}>Uji Koneksi</button>
                      <button type="button" className="btn btn-text btn-sm" disabled={busy !== null} onClick={() => onDiscover(p)}>Temukan Model</button>
                      <button type="button" className="btn btn-text btn-sm" disabled={busy !== null} onClick={() => onRefreshModels(p)}>Segarkan</button>
                    </div>

                    <h4 className="subhead">Model ({p.models.length})</h4>
                    {p.models.length === 0 ? (
                      <p className="muted"><small>Belum ada model — Temukan Model lalu tambahkan, atau ketik manual di bawah.</small></p>
                    ) : (
                      <ul className="mini-list model-list">
                        {p.models.map((m, mi) => (
                          <li key={m.id} className="model-row">
                            <span className="mono-sm muted">{mi + 1}.</span>
                            <span className="ellipsis" title={m.id}><strong>{m.id}</strong></span>
                            {p.unavailable_models.includes(m.id) && <span className="mini-chip mini-chip-failed">TIDAK TERSEDIA</span>}
                            {!m.enabled && <span className="mini-chip">NONAKTIF</span>}
                            <span className="model-ops">
                              <button type="button" className={`btn btn-icon btn-sm${m.enabled ? "" : " btn-danger-ghost"}`} onClick={() => toggleModel(p, m.id)} disabled={busy !== null} aria-label={m.enabled ? `Nonaktifkan ${m.id}` : `Aktifkan ${m.id}`} title={m.enabled ? "Nonaktifkan" : "Aktifkan"}>
                                <Icon name={m.enabled ? "check" : "close"} size={13} />
                              </button>
                              <button type="button" className="btn btn-icon btn-sm" onClick={() => moveModel(p, mi, -1)} disabled={busy !== null || mi === 0} aria-label="Naikkan model" title="Naikkan prioritas"><Icon name="chevU" size={13} /></button>
                              <button type="button" className="btn btn-icon btn-sm" onClick={() => moveModel(p, mi, 1)} disabled={busy !== null || mi === p.models.length - 1} aria-label="Turunkan model" title="Turunkan prioritas"><Icon name="chevD" size={13} /></button>
                              <button type="button" className="btn btn-icon btn-sm btn-danger-ghost" onClick={() => removeModel(p, m.id)} disabled={busy !== null} aria-label={`Hapus ${m.id}`} title="Hapus model"><Icon name="close" size={13} /></button>
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="row model-add">
                      <input
                        value={modelDraft[p.id] ?? ""}
                        onChange={(e) => setModelDraft((d) => ({ ...d, [p.id]: e.target.value }))}
                        placeholder="tambah model id…" aria-label={`Tambah model ke ${p.name}`}
                      />
                      <button type="button" className="btn btn-icon btn-sm" disabled={busy !== null} onClick={() => addModel(p)} aria-label="Tambah model" title="Tambah model"><Icon name="plus" size={14} /></button>
                    </div>

                    {discovered.length > 0 && (
                      <Collapsible title={`Hasil Temuan (${discovered.length})`} subtitle="Discovery terakhir" defaultOpen={false}>
                        <ul className="mini-list model-list">
                          {discovered.slice(0, 20).map((m) => (
                            <li key={m.id} className="model-row">
                              <span className="link-row-main">
                                <strong className="ellipsis" title={m.id}>{m.display_name || m.id}</strong>
                                {m.marked_unavailable && <span className="mini-chip mini-chip-failed">TIDAK TERSEDIA</span>}
                                {m.configured && <span className="mini-chip mini-chip-done">DIKONFIGURASI</span>}
                              </span>
                              {!m.configured && (
                                <button type="button" className="btn btn-tonal btn-sm" disabled={busy !== null} onClick={() => adoptDiscovered(p, m.id)}>Tambah</button>
                              )}
                            </li>
                          ))}
                        </ul>
                      </Collapsible>
                    )}

                    <Collapsible title="Lanjutan" subtitle="Uji model · sumber model · maintenance" defaultOpen={false}>
                      <div className="set-field">
                        <div className="set-label"><label>Model uji</label></div>
                        <div className="row">
                          <input
                            value={testModel[p.id] ?? p.models.find((m) => m.enabled)?.id ?? ""}
                            onChange={(e) => setTestModel((d) => ({ ...d, [p.id]: e.target.value }))}
                            placeholder="id model" aria-label={`Model uji untuk ${p.name}`}
                          />
                          <button type="button" className="btn btn-tonal btn-sm" disabled={busy !== null} onClick={() => onTestModel(p)}>Uji Model</button>
                        </div>
                      </div>

                      <h4 className="subhead">Sumber model</h4>
                      <div className="radio-row" role="radiogroup" aria-label={`Sumber model ${p.name}`}>
                        {(["manual", "automatic"] as const).map((m) => (
                          <label key={m} className="check">
                            <input
                              type="radio"
                              name={`mode-${p.id}`}
                              checked={(p.model_mode ?? "manual") === m}
                              disabled={busy !== null}
                              onChange={() => onSetMode(p, m)}
                            />
                            <span>{m === "manual" ? "Manual" : "Otomatis"}</span>
                          </label>
                        ))}
                      </div>
                      {(p.model_mode ?? "manual") === "automatic" ? (
                        <>
                          <div className="set-grid" style={{ marginTop: 8 }}>
                            <div className="set-field">
                              <div className="set-label"><label>Kedalaman rantai</label></div>
                              <input
                                type="number" min={1} max={10} step={1}
                                value={p.auto_depth ?? 4}
                                disabled={busy !== null}
                                onChange={(e) => onSetDepth(p, Number(e.target.value))}
                                aria-label={`Kedalaman rantai ${p.name}`}
                              />
                            </div>
                            <div className="set-field">
                              <div className="set-label"><label>Free tier</label></div>
                              <label className="check">
                                <input
                                  type="checkbox"
                                  checked={p.auto_free_tier_only ?? true}
                                  disabled={busy !== null}
                                  onChange={(e) => onSetFreeTier(p, e.target.checked)}
                                />
                                <span>Hanya free-tier</span>
                              </label>
                            </div>
                          </div>
                          <p className="muted">
                            <small>
                              Rantai di-resolve saat job berjalan dari discovery key ini.
                              {(() => {
                                const preview = selectableModels(
                                  (discovery[p.id] ?? []).filter((m) => !p.unavailable_models.includes(m.id)),
                                  p.auto_free_tier_only ?? true
                                ).slice(0, p.auto_depth ?? 4);
                                return preview.length > 0
                                  ? ` Pratinjau: ${preview.map((m) => m.id).join(" → ")}.`
                                  : " Jalankan Temukan Model untuk pratinjau.";
                              })()}
                              {" "}Bila discovery gagal, daftar manual di atas dipakai.
                            </small>
                          </p>
                        </>
                      ) : (
                        <p className="muted"><small>Daftar eksplisit di atas dipakai apa adanya.</small></p>
                      )}

                      <div className="prov-danger">
                        <button
                          type="button" className="btn btn-text btn-sm" disabled={busy !== null}
                          onClick={() => wrap(`reset-${p.id}`, () => resetProviderHealth(apiBase, p.id), "Kesehatan provider direset")}
                        >Atur Ulang Kesehatan</button>
                        <button
                          type="button" className="btn btn-text btn-sm btn-danger" disabled={busy !== null}
                          onClick={() => { if (window.confirm(`Hapus ${p.name}?`)) void wrap(`del-${p.id}`, () => deleteProvider(apiBase, p.id), "Provider dihapus"); }}
                        >Hapus Provider</button>
                      </div>
                    </Collapsible>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {showAdd && (
        <div className="dialog-scrim" onClick={() => busy === null && setShowAdd(false)} role="presentation">
          <div className="dialog" role="dialog" aria-modal="true" aria-label="Tambah Provider Gemini" onClick={(e) => e.stopPropagation()}>
            <h3 className="dialog-title">Tambah Provider Gemini</h3>
            <div className="form">
              <div className="field">
                <label htmlFor="prov-name">Nama Provider</label>
                <input id="prov-name" value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="Gemini API 6" autoComplete="off" />
              </div>
              <div className="field">
                <label htmlFor="prov-key">API Key</label>
                <input id="prov-key" value={newKey} onChange={(e) => setNewKey(e.target.value)} placeholder="AIza…" type="password" autoComplete="off" />
              </div>
              <p className="muted"><small>Setelah disimpan, jalankan Temukan Model di kartu provider untuk mengisi daftar model.</small></p>
            </div>
            <div className="dialog-actions">
              <button type="button" className="btn btn-text" onClick={() => setShowAdd(false)} disabled={busy !== null}>
                Batal
              </button>
              <button type="button" className="btn btn-filled" onClick={onAdd} disabled={busy !== null || !newName.trim() || !newKey.trim()} autoFocus>
                {busy === "add" ? "Menyimpan…" : "Simpan Provider"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
