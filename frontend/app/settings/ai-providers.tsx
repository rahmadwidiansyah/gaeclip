"use client";

import { useCallback, useEffect, useState } from "react";
import { Collapsible, ConfirmDialog, Icon, IconButton, Pagination, SearchInput } from "../components/ui";
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
import { buildProviderEditPayload, isDuplicateModel, validateProviderName } from "../lib/provider-forms";
import { DEFAULT_PAGE_SIZE, pageCount, paginate } from "../lib/pagination";
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

function filterDiscovery(models: ProviderDiscoveryModel[], query: string): ProviderDiscoveryModel[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return models;
  return models.filter((m) => `${m.id} ${m.display_name || ""}`.toLowerCase().includes(needle));
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
  const [discError, setDiscError] = useState<Record<string, string | null>>({});
  const [discQuery, setDiscQuery] = useState<Record<string, string>>({});
  const [discPage, setDiscPage] = useState<Record<string, number>>({});
  const [discSize, setDiscSize] = useState<Record<string, number>>({});
  const [newName, setNewName] = useState("");
  const [newKey, setNewKey] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [modelDraft, setModelDraft] = useState<Record<string, string>>({});
  const [testModel, setTestModel] = useState<Record<string, string>>({});
  // Accordion by stable provider id: SEMUA COLLAPSED by default, maksimal
  // satu expanded. UI state saja (tidak disimpan ke database).
  const [openId, setOpenId] = useState<string | null>(null);

  // Edit dialog state — identified by STABLE id; name is display-only and
  // freely renameable. The key field starts BLANK: blank = keep old secret.
  const [editTarget, setEditTarget] = useState<GeminiProviderInfo | null>(null);
  const [editName, setEditName] = useState("");
  const [editKey, setEditKey] = useState("");
  const [editEnabled, setEditEnabled] = useState(true);
  const [editPriority, setEditPriority] = useState(1);

  // Delete confirmation — never window.confirm(); uses shared Dialog.
  const [deleteTarget, setDeleteTarget] = useState<GeminiProviderInfo | null>(null);

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

  const openEdit = (p: GeminiProviderInfo) => {
    setEditTarget(p);
    setEditName(p.name);
    setEditKey("");
    setEditEnabled(p.enabled);
    setEditPriority(p.priority);
  };

  const editNameError = validateProviderName(editName);

  const onSaveEdit = async () => {
    if (!editTarget || editNameError) return;
    const key = `edit-${editTarget.id}`;
    setBusy(key);
    try {
      // Blank key field => payload omits api_key => secret preserved.
      await editProvider(apiBase, editTarget.id, buildProviderEditPayload({
        name: editName,
        apiKey: editKey,
        enabled: editEnabled,
        priority: editPriority,
      }));
      await load();
      setEditTarget(null);
      push(`Provider "${editName.trim()}" disimpan`, "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal menyimpan provider", "error");
    } finally {
      setBusy(null);
    }
  };

  const onConfirmDelete = () =>
    wrap(`del-${deleteTarget?.id ?? ""}`, async () => {
      if (!deleteTarget) throw new Error("Provider tidak dipilih");
      await deleteProvider(apiBase, deleteTarget.id);
      // Direct state update: no manual page reload needed; wrap() reloads
      // the authoritative list right after.
      setProviders((prev) => (prev ? prev.filter((p) => p.id !== deleteTarget.id) : prev));
      setOpenId((prev) => (prev === deleteTarget.id ? null : prev));
      setDeleteTarget(null);
    }, "Provider dihapus");

  const resetDiscoveryPage = (id: string) => setDiscPage((s) => ({ ...s, [id]: 1 }));

  const onDiscover = (p: GeminiProviderInfo) =>
    wrap(`discover-${p.id}`, async () => {
      setDiscError((d) => ({ ...d, [p.id]: null }));
      try {
        const res = await getProviderModels(apiBase, p.id);
        // Replace (never merge): stale results must not mix with fresh ones.
        setDiscovery((d) => ({ ...d, [p.id]: res.models ?? [] }));
        resetDiscoveryPage(p.id);
      } catch (e) {
        setDiscError((d) => ({ ...d, [p.id]: e instanceof Error ? e.message : "Discovery gagal" }));
        throw e;
      }
    });

  const onRefreshModels = (p: GeminiProviderInfo) =>
    wrap(`refresh-${p.id}`, async () => {
      setDiscError((d) => ({ ...d, [p.id]: null }));
      try {
        const res = await refreshProviderModels(apiBase, p.id);
        const removed: string[] = res.removed_marked_unavailable ?? [];
        push(
          removed.length > 0
            ? `Discovery: ${res.discovered ?? 0} model, ${removed.length} ditandai tidak tersedia`
            : `Discovery: ${res.discovered ?? 0} model`,
          "success"
        );
        await onDiscoverSilent(p.id);
        resetDiscoveryPage(p.id);
      } catch (e) {
        setDiscError((d) => ({ ...d, [p.id]: e instanceof Error ? e.message : "Refresh discovery gagal" }));
        throw e;
      }
    });

  const onDiscoverSilent = async (id: string) => {
    try {
      const res = await getProviderModels(apiBase, id);
      setDiscovery((d) => ({ ...d, [id]: res.models ?? [] }));
      setDiscError((d) => ({ ...d, [id]: null }));
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

  const deleteTargetActive =
    deleteTarget !== null &&
    deleteTarget.enabled &&
    deleteTarget.key_configured &&
    deleteTarget.models.some((m) => m.enabled);

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
            const allDiscovered = discovery[p.id] ?? [];
            const query = discQuery[p.id] ?? "";
            const filtered = filterDiscovery(allDiscovered, query);
            const pageSize = discSize[p.id] ?? DEFAULT_PAGE_SIZE;
            const totalPages = pageCount(filtered.length, pageSize);
            const discPageSafe = Math.min(totalPages, Math.max(1, discPage[p.id] ?? 1));
            const visible = paginate(filtered, discPageSafe, pageSize);
            const discovering = busy === `discover-${p.id}` || busy === `refresh-${p.id}`;
            const open = openId === p.id;
            const enabledModels = p.models.filter((m) => m.enabled).length;
            const lastOk = timeAgo(p.health?.last_success);
            const draftId = (modelDraft[p.id] ?? "").trim();
            const draftDuplicate = isDuplicateModel(p.models.map((m) => m.id), draftId);
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
                  <span className="prov-head-actions" onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                    <IconButton icon="edit" label={`Edit provider ${p.name}`} title="Edit provider" onClick={() => openEdit(p)} disabled={busy !== null} />
                    <IconButton icon="trash" label={`Hapus provider ${p.name}`} title="Hapus provider" variant="danger" onClick={() => setDeleteTarget(p)} disabled={busy !== null} />
                  </span>
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
                          <IconButton icon="chevU" label="Naikkan prioritas provider" title="Naikkan prioritas" onClick={() => moveProvider(pi, -1)} disabled={busy !== null || pi === 0} />
                          <IconButton icon="chevD" label="Turunkan prioritas provider" title="Turunkan prioritas" onClick={() => moveProvider(pi, 1)} disabled={busy !== null || pi === providers.length - 1} />
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
                      <button type="button" className="btn btn-text btn-sm" disabled={busy !== null} onClick={() => openEdit(p)}>Edit Provider</button>
                      <button type="button" className="btn btn-text btn-sm" disabled={busy !== null} onClick={() => onDiscover(p)}>{discovering ? "Mencari…" : "Temukan Model"}</button>
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
                              <IconButton
                                icon={m.enabled ? "check" : "close"}
                                label={m.enabled ? `Nonaktifkan ${m.id}` : `Aktifkan ${m.id}`}
                                title={m.enabled ? "Nonaktifkan" : "Aktifkan"}
                                onClick={() => toggleModel(p, m.id)}
                                disabled={busy !== null}
                              />
                              <IconButton icon="chevU" label={`Naikkan prioritas ${m.id}`} title="Naikkan prioritas" onClick={() => moveModel(p, mi, -1)} disabled={busy !== null || mi === 0} />
                              <IconButton icon="chevD" label={`Turunkan prioritas ${m.id}`} title="Turunkan prioritas" onClick={() => moveModel(p, mi, 1)} disabled={busy !== null || mi === p.models.length - 1} />
                              <IconButton icon="trash" label={`Hapus ${m.id}`} title="Hapus model" variant="danger" onClick={() => removeModel(p, m.id)} disabled={busy !== null} />
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                    <div className="row model-add-row">
                      <input
                        value={modelDraft[p.id] ?? ""}
                        onChange={(e) => setModelDraft((d) => ({ ...d, [p.id]: e.target.value }))}
                        onKeyDown={(e) => { if (e.key === "Enter" && draftId && !draftDuplicate) addModel(p); }}
                        placeholder="tambah model id…"
                        aria-label={`Tambah model ke ${p.name}`}
                      />
                      <IconButton
                        icon="plus"
                        label={draftDuplicate ? "Model sudah ada di provider ini" : `Tambah model ke ${p.name}`}
                        title={draftDuplicate ? "Model sudah ada" : "Tambah model"}
                        variant="primary"
                        onClick={() => addModel(p)}
                        disabled={busy !== null || !draftId || draftDuplicate}
                      />
                    </div>
                    {draftDuplicate && (
                      <p className="muted"><small>Model sudah ada di provider ini.</small></p>
                    )}

                    {(allDiscovered.length > 0 || discError[p.id]) && (
                      <Collapsible title={`Hasil Temuan (${allDiscovered.length})`} subtitle="Discovery terakhir" defaultOpen={false}>
                        <div className="discovery-toolbar">
                          <SearchInput
                            value={query}
                            onChange={(v) => { setDiscQuery((s) => ({ ...s, [p.id]: v })); resetDiscoveryPage(p.id); }}
                            placeholder="Cari model…"
                            ariaLabel={`Cari hasil temuan ${p.name}`}
                          />
                          <span className="discovery-count" role="status">
                            {query.trim()
                              ? `${filtered.length} dari ${allDiscovered.length} model`
                              : `${allDiscovered.length} model`}
                            {discovering ? " · memuat…" : ""}
                          </span>
                        </div>
                        {discError[p.id] && (
                          <p className="banner banner-error" role="alert">{discError[p.id]}</p>
                        )}
                        {filtered.length === 0 ? (
                          <p className="muted"><small>{query.trim() ? "Tidak ada model yang cocok dengan pencarian." : "Belum ada hasil discovery."}</small></p>
                        ) : (
                          <>
                            <ul className="mini-list model-list">
                              {visible.map((m) => (
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
                            <div className="discovery-foot">
                              <span className="discovery-count">
                                Menampilkan {visible.length > 0 ? `${(discPageSafe - 1) * pageSize + 1}–${(discPageSafe - 1) * pageSize + visible.length} dari ${filtered.length}` : "0"}
                              </span>
                              <Pagination
                                page={discPageSafe}
                                totalPages={totalPages}
                                total={filtered.length}
                                pageSize={pageSize}
                                onPage={(n) => setDiscPage((s) => ({ ...s, [p.id]: n }))}
                                onPageSizeChange={(n) => { setDiscSize((s) => ({ ...s, [p.id]: n })); resetDiscoveryPage(p.id); }}
                              />
                            </div>
                          </>
                        )}
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

      {editTarget && (
        <div className="dialog-scrim" onClick={() => busy === null && setEditTarget(null)} role="presentation">
          <div className="dialog" role="dialog" aria-modal="true" aria-label={`Edit provider ${editTarget.name}`} onClick={(e) => e.stopPropagation()}>
            <h3 className="dialog-title">Edit Provider</h3>
            <p className="hint">ID internal <span className="mono-sm">{editTarget.id}</span> tetap stabil — nama bebas diubah tanpa merusak referensi.</p>
            <div className="form">
              <div className="field">
                <label htmlFor="edit-prov-name">Nama Provider</label>
                <input id="edit-prov-name" value={editName} onChange={(e) => setEditName(e.target.value)} placeholder="Gemini Utama" autoComplete="off" />
                {editNameError && <p className="field-err" role="alert">{editNameError}</p>}
              </div>
              <div className="field">
                <label htmlFor="edit-prov-key">API Key {editTarget.key_configured && <span className="mini-chip">••••{editTarget.key_suffix}</span>}</label>
                <input
                  id="edit-prov-key"
                  value={editKey}
                  onChange={(e) => setEditKey(e.target.value)}
                  placeholder="Kosongkan untuk mempertahankan key lama"
                  type="password"
                  autoComplete="new-password"
                />
                <p className="hint">Dikosongkan = secret lama dipertahankan. Key tidak pernah ditampilkan utuh.</p>
              </div>
              <div className="field">
                <div className="set-label"><label htmlFor="edit-prov-priority">Prioritas</label></div>
                <input
                  id="edit-prov-priority"
                  type="number" min={1} step={1}
                  value={editPriority}
                  onChange={(e) => setEditPriority(Number(e.target.value))}
                />
              </div>
              <label className="check check-row">
                <input type="checkbox" checked={editEnabled} onChange={(e) => setEditEnabled(e.target.checked)} />
                <span>Aktifkan provider</span>
              </label>
            </div>
            <div className="dialog-actions">
              <button type="button" className="btn btn-text" onClick={() => setEditTarget(null)} disabled={busy !== null}>
                Batal
              </button>
              <button
                type="button"
                className="btn btn-filled"
                onClick={onSaveEdit}
                disabled={busy !== null || !!editNameError}
                autoFocus
              >
                {busy === `edit-${editTarget.id}` ? "Menyimpan…" : "Simpan Perubahan"}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Hapus provider?"
        message={
          deleteTarget
            ? `Provider "${deleteTarget.name}" dan konfigurasi model fallback-nya akan dihapus.${deleteTargetActive ? " Provider ini sedang AKTIF dan dipakai konfigurasi berjalan." : ""}`
            : ""
        }
        confirmLabel="Hapus"
        danger
        busy={busy !== null}
        onConfirm={onConfirmDelete}
        onClose={() => busy === null && setDeleteTarget(null)}
      />
    </section>
  );
}
