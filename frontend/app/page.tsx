"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Collapsible, Icon, PageHeader, ProgressBar, Stat, StatusChip } from "./components/ui";
import { checkHealth, createJob, getAppSettings, resolveBrandAvatarUrl, uploadBrandAvatar } from "./lib/api";
import { useJobs } from "./lib/hooks";
import { useSettings, useToast } from "./lib/store";
import { ACTIVE_STATES, SUBTITLE_POSITIONS, SUBTITLE_STYLES } from "./lib/types";
import type { SubtitlePosition, SubtitleStyle } from "./lib/types";

import { resolveSubtitlePreview } from "./lib/subtitles";
import {
  BRAND_ANIMATIONS,
  BRAND_DISPLAY_MODES,
  BRAND_DURATIONS,
  BRAND_POSITIONS,
  BRAND_TEMPLATES,
  BRAND_TIMINGS,
  DEFAULT_BRANDING,
  brandWindowLabel,
  clampBrandDuration,
  isCornerTemplate,
  isEndcardTemplate,
  isProbablyYoutubeUrl,
  resolveBrandingPreview,
} from "./lib/branding";
import type { BrandingForm, BrandTemplate } from "./lib/branding";

function SubtitlePreview({ style, position }: { style: SubtitleStyle; position: SubtitlePosition }) {
  // Single source: layout/position mirror the burn renderer (see lib/subtitles.ts).
  // The frame is a column flexbox, so justifyContent places text VERTICALLY —
  // the old row-direction flexbox ignored it and pinned text to the top.
  const spec = resolveSubtitlePreview(style, position);
  const { layout, render } = spec;
  const o = render.outlineEm.toFixed(4);
  const d = (render.outlineEm / 2).toFixed(4);
  // em-based stroke scales with font-size (true px outline would not survive preview scaling).
  const strokes = [
    `-${o}em 0 0 #000`, `${o}em 0 0 #000`, `0 -${o}em 0 #000`, `0 ${o}em 0 #000`,
    `-${d}em -${d}em 0 #000`, `${d}em -${d}em 0 #000`, `-${d}em ${d}em 0 #000`, `${d}em ${d}em 0 #000`,
  ].join(", ");
  const shadow = render.shadow ? `, 0 0.06em 0.14em rgba(0,0,0,0.85)` : "";
  const sample = style === "dynamic_highlight" ? ["Ini contoh", "kata penting"] : ["Ini contoh subtitle"];
  return (
    <div className="sub-preview" aria-label="Pratinjau style subtitle">
      <div
        className="sub-preview-frame"
        style={{
          justifyContent: layout.justify,
          paddingBottom: `${spec.padBottomCssPct}%`,
          paddingInline: `${spec.sideInsetPct}%`,
        }}
      >
        <div
          style={{
            width: "100%",
            textAlign: "center",
            color: render.color,
            fontFamily: render.fontFamily,
            fontWeight: render.fontWeight,
            textTransform: render.textTransform,
            fontSize: `max(${spec.fontPxFallback}px, ${spec.fontCqw.toFixed(4)}cqw)`,
            lineHeight: 1.35,
            textShadow: strokes + shadow,
            overflowWrap: "break-word",
          }}
        >
          {sample.map((line, i) => (
            <div key={i}>
              {style === "dynamic_highlight" && i === 1 ? (
                <>
                  kata <span style={{ color: "#00ff00" }}>penting</span>
                </>
              ) : (
                line
              )}
            </div>
          ))}
        </div>
      </div>
      <small className="muted">Pratinjau mendekati hasil burn (font/warna/posisi sama, tanpa karaoke animasi).</small>
    </div>
  );
}

function BrandingPreview({ form, apiBase, compact = false }: { form: BrandingForm; apiBase: string; compact?: boolean }) {
  // CSS simulation of the FFmpeg render (see lib/branding.ts): same 9:16
  // frame, positions and blur backdrop are approximated, not identical.
  const spec = resolveBrandingPreview(form);
  const align: Record<string, "flex-start" | "flex-end" | "center"> = {
    top_left: "flex-start",
    top_right: "flex-end",
    bottom_left: "flex-start",
    bottom_right: "flex-end",
    top_center: "center",
    center: "center",
  };
  const avatarSrc = form.avatarPreviewUrl
    ? form.avatarPreviewUrl.startsWith("http")
      ? form.avatarPreviewUrl
      : `${apiBase}${form.avatarPreviewUrl}`
    : null;
  return (
    <div className="sub-preview" aria-label={spec.ariaLabel}>
      <div
        className="sub-preview-frame"
        style={{
          justifyContent: spec.justify,
          ...(compact ? { maxWidth: 76 } : null),
          ...(spec.hasBlurBg
            ? { background: "linear-gradient(160deg, rgba(30,27,75,0.92), rgba(15,12,40,0.94))" }
            : null),
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: align[form.position] ?? "flex-start",
            gap: "0.5em",
            width: "100%",
            padding: "0.6em",
            fontSize: compact ? 8 : undefined,
          }}
        >
          {spec.showAvatar &&
            (avatarSrc ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={avatarSrc}
                alt=""
                style={{ width: `${spec.avatarPct}%`, aspectRatio: "1/1", borderRadius: "999px", objectFit: "cover", border: "1px solid rgba(255,255,255,0.7)" }}
              />
            ) : (
              <span
                aria-hidden="true"
                style={{
                  width: `${spec.avatarPct}%`,
                  aspectRatio: "1/1",
                  borderRadius: "999px",
                  display: "grid",
                  placeItems: "center",
                  background: "rgba(255,255,255,0.25)",
                  color: "#fff",
                  fontWeight: 700,
                  border: "1px solid rgba(255,255,255,0.7)",
                }}
              >
                {spec.fallbackInitial}
              </span>
            ))}
          {spec.showName && (
            <span
              style={{
                color: "#fff",
                fontWeight: 600,
                fontSize: "0.95em",
                textShadow: "0 1px 2px rgba(0,0,0,0.8), 0 0 1px #000",
                overflowWrap: "anywhere",
              }}
            >
              {form.channelName.trim() || "Nama channel"}
            </span>
          )}
        </div>
      </div>
      {!compact && (
        <small className="muted">Simulasi preview — blur dan animasi hasil render bisa sedikit berbeda.</small>
      )}
    </div>
  );
}

function BrandingSection({
  apiBase,
  push,
  form,
  onChange,
  sourceUrl,
}: {
  apiBase: string;
  push: (message: string, kind?: "info" | "success" | "error") => void;
  form: BrandingForm;
  onChange: (patch: Partial<BrandingForm>) => void;
  sourceUrl: string;
}) {
  const [working, setWorking] = useState(false);
  const [metaMsg, setMetaMsg] = useState<string | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const resolvedFor = useRef("");
  const seq = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  // Auto identity: the pasted video URL resolves to its channel (name +
  // avatar) via yt-dlp metadata — no manual identity work in the normal
  // flow. Debounced, stale runs ignored, in-flight run aborted on change.
  useEffect(() => {
    if (!form.enabled) return;
    const target = (sourceUrl || "").trim();
    if (!isProbablyYoutubeUrl(target) || target === resolvedFor.current) return;
    const mySeq = ++seq.current;
    resolvedFor.current = target;
    onChange({ channelName: "", avatarAsset: "", avatarPreviewUrl: "" });
    setMetaMsg("Mengenali channel dari URL…");
    setMetaError(null);
    const timer = setTimeout(() => {
      const ctrl = new AbortController();
      abortRef.current = ctrl;
      setWorking(true);
      (async () => {
        try {
          const data = await resolveBrandAvatarUrl(apiBase, target, { signal: ctrl.signal });
          if (seq.current !== mySeq) return;
          onChange({
            channelName: data.channel_name || "",
            avatarAsset: data.asset_id || "",
            avatarPreviewUrl: data.preview_url || "",
          });
          if (!data.channel_name) {
            setMetaError("Channel tidak dikenali dari URL ini.");
            setMetaMsg(null);
          } else if (!data.asset_id) {
            setMetaMsg(`Channel: ${data.channel_name} — avatar tidak ditemukan, memakai inisial.`);
            setMetaError(null);
          } else {
            setMetaMsg(`Channel: ${data.channel_name}`);
            setMetaError(null);
          }
        } catch (e) {
          if (seq.current !== mySeq) return;
          if (e instanceof DOMException && e.name === "AbortError") return;
          setMetaError(e instanceof Error ? e.message : "Gagal mengenali channel");
          setMetaMsg(null);
        } finally {
          if (seq.current === mySeq) setWorking(false);
        }
      })();
    }, 800);
    return () => {
      clearTimeout(timer);
      abortRef.current?.abort();
      abortRef.current = null;
      seq.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, sourceUrl, form.enabled, retryTick]);

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    setWorking(true);
    try {
      const asset = await uploadBrandAvatar(apiBase, file);
      onChange({ avatarAsset: asset.asset_id, avatarPreviewUrl: asset.preview_url });
      push("Avatar diunggah", "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal mengunggah avatar", "error");
    } finally {
      setWorking(false);
    }
  };

  const endcard = isEndcardTemplate(form.template);
  const corner = isCornerTemplate(form.template);
  const set = (patch: Partial<BrandingForm>) => onChange(patch);

  return (
    <Collapsible title="Clip Branding" subtitle="Identitas channel di video" badge={form.enabled ? "Aktif" : undefined} defaultOpen={false}>
      <label className="check" htmlFor="brand-on">
        <input id="brand-on" type="checkbox" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
        <span>
          Aktifkan branding
          <small>Avatar + nama channel dirender ke video final</small>
        </span>
      </label>
      {form.enabled && (
        <>
          <div className="field" style={{ marginTop: 8 }}>
            <span id="brand-template">Template</span>
            <div className="plat-grid" role="group" aria-labelledby="brand-template">
              {BRAND_TEMPLATES.map((t) => {
                const pressed = form.template === t.id;
                return (
                  <button
                    key={t.id}
                    type="button"
                    className="plat-card"
                    aria-pressed={pressed}
                    aria-label={`${t.label}: ${t.hint}${pressed ? " (dipilih)" : ""}`}
                    title={`${t.label} — ${t.hint}`}
                    onClick={() => set({ template: t.id as BrandTemplate })}
                  >
                    <span className="plat-check" aria-hidden="true">
                      <Icon name="check" size={12} />
                    </span>
                    <Icon name={t.id === "glass_end" ? "palette" : t.id === "minimal_corner" ? "type" : t.id === "creator_tag" ? "badge" : "eye"} size={20} />
                    <span className="plat-name">{t.label}</span>
                    <span className="plat-status">{t.hint}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <div className="field" role="status" aria-label="Identitas channel">
            <span>Channel</span>
            {working && <small className="muted">Mengenali channel dari URL…</small>}
            {!working && metaMsg && <small className="muted">{metaMsg}</small>}
            {!working && metaError && (
              <small className="muted">
                {metaError}{" "}
                <button type="button" className="btn btn-text btn-sm" onClick={() => setRetryTick((n) => n + 1)}>
                  Coba lagi
                </button>
              </small>
            )}
            {!working && !metaMsg && !metaError && (
              <small className="muted">Tempel URL YouTube yang valid untuk mengenali channel otomatis.</small>
            )}
          </div>
          <div className="field-row">
            <div className="field">
              <label htmlFor="brand-display">Tampilan</label>
              <select
                id="brand-display"
                value={form.displayMode}
                onChange={(e) => set({ displayMode: e.target.value as BrandingForm["displayMode"] })}
              >
                {BRAND_DISPLAY_MODES.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <details>
            <summary>Opsi lanjutan (manual)</summary>
            <p className="muted">
              <small>Hanya bila channel tidak dikenali otomatis. Identitas manual tidak menimpa hasil otomatis kecuali diisi.</small>
            </p>
            <div className="field">
              <label htmlFor="brand-name">Nama channel manual</label>
              <input
                id="brand-name"
                type="text"
                maxLength={60}
                value={form.channelName}
                onChange={(e) => set({ channelName: e.target.value })}
                placeholder="Nama channel kreator"
                disabled={working}
              />
            </div>
            <div className="field">
              <label htmlFor="brand-avatar">Avatar manual {working ? "(memproses…)" : ""}</label>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <input
                  id="brand-avatar"
                  type="file"
                  accept="image/png,image/jpeg"
                  disabled={working}
                  onChange={(e) => void onFile(e.target.files?.[0])}
                />
              </div>
              <small className="muted">PNG/JPEG ≤ 2 MB.</small>
            </div>
          </details>
          {corner && (
            <div className="field-row">
              <div className="field">
                <label htmlFor="brand-pos" title="Sudut tempat branding tampil">Posisi</label>
                <select id="brand-pos" value={form.position} onChange={(e) => set({ position: e.target.value as BrandingForm["position"] })}>
                  {BRAND_POSITIONS.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="brand-timing">Durasi tampil</label>
                <select id="brand-timing" value={form.timing} onChange={(e) => set({ timing: e.target.value as BrandingForm["timing"] })}>
                  {BRAND_TIMINGS.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
          {(endcard || form.template === "creator_outro" || form.timing !== "full") && (
            <div className="field">
              <label htmlFor="brand-dur">Durasi end card ({brandWindowLabel(form)})</label>
              <select
                id="brand-dur"
                value={String(form.duration)}
                onChange={(e) => set({ duration: clampBrandDuration(Number(e.target.value)) })}
              >
                {[1, 1.5, 2, 3].includes(form.duration) ? null : (
                  <option value={String(form.duration)}>{form.duration} detik</option>
                )}
                {BRAND_DURATIONS.map((d) => (
                  <option key={d} value={String(d)}>
                    {d} detik
                  </option>
                ))}
              </select>
            </div>
          )}
          {endcard && (
            <div className="field-row">
              <div className="field">
                <label htmlFor="brand-blur" title="Kekuatan blur background end card">Blur background</label>
                <select id="brand-blur" value={String(form.blur)} onChange={(e) => set({ blur: Number(e.target.value) })}>
                  {[
                    [10, "Ringan"],
                    [20, "Sedang"],
                    [30, "Kuat"],
                    [40, "Sangat kuat"],
                  ].map(([v, label]) => (
                    <option key={String(v)} value={String(v)}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="brand-dim" title="Seberapa gelap lapisan di atas background">Kegelapan</label>
                <select id="brand-dim" value={String(form.dim)} onChange={(e) => set({ dim: Number(e.target.value) })}>
                  {[
                    [0.2, "Terang"],
                    [0.35, "Sedang"],
                    [0.5, "Gelap"],
                    [0.65, "Sangat gelap"],
                  ].map(([v, label]) => (
                    <option key={String(v)} value={String(v)}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          )}
          <div className="field-row">
            <div className="field">
              <label htmlFor="brand-anim">Animasi</label>
              <select id="brand-anim" value={form.animation} onChange={(e) => set({ animation: e.target.value as BrandingForm["animation"] })}>
                {BRAND_ANIMATIONS.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label}
                  </option>
                ))}
              </select>
            </div>
            {form.template === "creator_outro" && (
              <div className="field">
                <label htmlFor="brand-add">Teks tambahan (opsional)</label>
                <input
                  id="brand-add"
                  type="text"
                  maxLength={40}
                  value={form.additionalText}
                  onChange={(e) => set({ additionalText: e.target.value })}
                  placeholder="cth: Subscribe"
                />
              </div>
            )}
          </div>
          <BrandingPreview form={form} apiBase={apiBase} />
        </>
      )}
    </Collapsible>
  );
}

export default function Dashboard() {
  const router = useRouter();
  const { apiBase } = useSettings();
  const push = useToast();
  const { jobs, loading, error, refresh } = useJobs(100);

  const [url, setUrl] = useState("");
  const [language, setLanguage] = useState("id");
  const [maxClips, setMaxClips] = useState(3);
  const [minDuration, setMinDuration] = useState(30);
  const [maxDuration, setMaxDuration] = useState(60);
  const [burnSubs, setBurnSubs] = useState(true);
  const [subStyle, setSubStyle] = useState<SubtitleStyle>("casual_center");
  const [subPos, setSubPos] = useState<SubtitlePosition>("bottom_center");
  const [subMaxWords, setSubMaxWords] = useState(3);
  const [brand, setBrand] = useState<BrandingForm>(DEFAULT_BRANDING);
  const [busy, setBusy] = useState(false);
  const [health, setHealth] = useState<{ api: boolean; worker: boolean } | null>(null);

  const active = jobs.filter((j) => ACTIVE_STATES.includes(j.status));
  const done = jobs.filter((j) => ["completed", "partial"].includes(j.status));
  const failed = jobs.filter((j) => j.status === "failed");
  const clips = jobs.reduce((n, j) => n + (j.clips?.length ?? 0), 0);

  const ping = async () => {
    const h = await checkHealth(apiBase);
    setHealth(h);
  };

  useEffect(() => {
    ping();
    // Server-side defaults drive the create form (single source of truth).
    getAppSettings(apiBase)
      .then((list) => {
        const find = (key: string) => list.find((e) => e.key === key)?.value;
        const mc = Number(find("MAX_CLIPS"));
        const lo = Number(find("MIN_CLIP_DURATION_SECONDS"));
        const hi = Number(find("MAX_CLIP_DURATION_SECONDS"));
        if (isFinite(mc) && mc > 0) setMaxClips(mc);
        if (isFinite(lo) && lo > 0) setMinDuration(lo);
        if (isFinite(hi) && hi > 0) setMaxDuration(hi);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase]);

  const submit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (minDuration > maxDuration) {
      push("Durasi min tidak boleh melebihi max", "error");
      return;
    }
    setBusy(true);
    try {
      if (brand.enabled) {
        const needsName = brand.displayMode !== "avatar_only";
        if (needsName && !brand.channelName.trim()) {
          push("Isi nama channel untuk branding, atau pilih mode avatar saja", "error");
          setBusy(false);
          return;
        }
        if (brand.displayMode === "avatar_only" && !brand.avatarAsset) {
          push("Unggah atau ambil avatar dulu untuk mode avatar saja", "error");
          setBusy(false);
          return;
        }
      }
      const job = await createJob(apiBase, {
        source_url: url,
        language,
        max_clips: maxClips,
        min_duration: minDuration,
        max_duration: maxDuration,
        burn_subtitles: burnSubs,
        subtitle_style: subStyle,
        subtitle_position: subPos,
        subtitle_max_words_per_line: Math.min(3, Math.max(1, Math.round(subMaxWords) || 3)),
        brand_enabled: brand.enabled,
        brand_template: brand.template,
        brand_channel_name: brand.channelName.trim(),
        brand_avatar_asset: brand.avatarAsset,
        brand_display_mode: brand.displayMode,
        brand_position: brand.position,
        brand_timing: brand.timing,
        brand_duration: clampBrandDuration(brand.duration),
        brand_blur: brand.blur,
        brand_dim: brand.dim,
        brand_animation: brand.animation,
        brand_additional_text: brand.additionalText.trim(),
      });
      push("Job dibuat, mengalihkan ke detail…", "success");
      setUrl("");
      refresh();
      router.push(`/jobs/${job.job_id}`);
    } catch (err) {
      push(err instanceof Error ? err.message : "Gagal membuat job", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="GAECLIP STUDIO"
        title="Dashboard"
        desc="Ringkasan, project baru, dan status sistem."
        actions={
          <button type="button" className="btn btn-tonal btn-sm" onClick={() => { refresh(); ping(); }}>
            <Icon name="refresh" size={15} /> Muat ulang
          </button>
        }
      />

      {error && (
        <div className="banner banner-error" role="alert">
          <Icon name="alert" size={16} /> {error}
        </div>
      )}

      <div className="stat-row">
        <Stat icon="jobs" label="Job aktif" value={loading ? "…" : active.length} tone="info" />
        <Stat icon="check" label="Selesai" value={loading ? "…" : done.length} tone="ok" />
        <Stat icon="clips" label="Total clip" value={loading ? "…" : clips} tone="default" />
        <Stat icon="alert" label="Gagal" value={loading ? "…" : failed.length} tone={failed.length ? "err" : "default"} />
      </div>

      <div className="grid-2">
        <section className="card" aria-label="Buat project baru">
          <div className="card-head">
            <h2>Project baru</h2>
            <p>Tempel link YouTube, atur, lalu buat clip.</p>
          </div>
          <form onSubmit={submit} className="form">
            <div className="field">
              <label htmlFor="url">Link video YouTube</label>
              <input
                id="url"
                type="url"
                required
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="https://youtube.com/watch?v=…"
              />
            </div>
            <div className="field-row">
              <div className="field">
                <label htmlFor="lang">Bahasa</label>
                <select id="lang" value={language} onChange={(e) => setLanguage(e.target.value)}>
                  <option value="id">Indonesia</option>
                  <option value="en">English</option>
                </select>
              </div>
              <div className="field">
                <label htmlFor="nclip">Jumlah clip</label>
                <select id="nclip" value={maxClips} onChange={(e) => setMaxClips(Number(e.target.value))}>
                  {[...new Set([1, 2, 3, 4, 5, 6, 8, 10, maxClips])].sort((a, b) => a - b).map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="dmin">Durasi min</label>
                <select id="dmin" value={minDuration} onChange={(e) => setMinDuration(Number(e.target.value))}>
                  {[...new Set([15, 20, 30, 45, minDuration])].sort((a, b) => a - b).map((n) => (
                    <option key={n} value={n}>
                      {n} dtk
                    </option>
                  ))}
                </select>
              </div>
              <div className="field">
                <label htmlFor="dmax">Durasi maks</label>
                <select id="dmax" value={maxDuration} onChange={(e) => setMaxDuration(Number(e.target.value))}>
                  {[...new Set([30, 45, 60, 90, maxDuration])].sort((a, b) => a - b).map((n) => (
                    <option key={n} value={n}>
                      {n} dtk
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <label className="check" htmlFor="burn">
              <input id="burn" type="checkbox" checked={burnSubs} onChange={(e) => setBurnSubs(e.target.checked)} />
              <span>
                Bakar subtitle ke video
                <small>Matikan untuk clip tanpa teks</small>
              </span>
            </label>
            {burnSubs && (
              <div className="sub-config">
                <div className="sub-config-fields">
                  <div className="field">
                    <label htmlFor="substyle">Subtitle style</label>
                    <select id="substyle" value={subStyle} onChange={(e) => setSubStyle(e.target.value as SubtitleStyle)}>
                      {SUBTITLE_STYLES.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="subpos">Subtitle position</label>
                    <select id="subpos" value={subPos} onChange={(e) => setSubPos(e.target.value as SubtitlePosition)}>
                      {SUBTITLE_POSITIONS.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="field">
                    <label htmlFor="subwords" title="Maksimal kata per baris subtitle (1–3)">Max words per line</label>
                    <select id="subwords" value={subMaxWords} onChange={(e) => setSubMaxWords(Number(e.target.value))}>
                      {[1, 2, 3].map((n) => (
                        <option key={n} value={n}>
                          {n} kata
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                <SubtitlePreview style={subStyle} position={subPos} />
              </div>
            )}
            <BrandingSection
              apiBase={apiBase}
              push={push}
              form={brand}
              onChange={(patch) => setBrand((b) => ({ ...b, ...patch }))}
              sourceUrl={url}
            />
            <button type="submit" className="btn btn-filled" disabled={busy || minDuration > maxDuration}>
              <Icon name="plus" size={15} /> {busy ? "Menyiapkan…" : "Buat clips"}
            </button>
          </form>
        </section>

        <section className="card" aria-label="Status sistem">
          <div className="card-head">
            <h2>Status sistem</h2>
            <p>Kesehatan API & worker.</p>
          </div>
          <ul className="kv">
            <li>
              <span>API</span>
              <span className={`dot dot-${health ? (health.api ? "ok" : "err") : "idle"}`} aria-label={health ? (health.api ? "online" : "offline") : "unknown"}>
                {health ? (health.api ? "Online" : "Offline") : "…"}
              </span>
            </li>
            <li>
              <span>Worker</span>
              <span className={`dot dot-${health ? (health.worker ? "ok" : "err") : "idle"}`}>
                {health ? (health.worker ? "Siap" : "Bermasalah") : "…"}
              </span>
            </li>
            <li className="sys-endpoint">
              <span>Endpoint</span>
              <code className="mono ellipsis" title={apiBase}>{apiBase}</code>
            </li>
          </ul>
          <h3 className="subhead">Job aktif ({active.length})</h3>
          {active.length === 0 ? (
            <p className="muted">Tidak ada job berjalan.</p>
          ) : (
            <ul className="mini-list">
              {active.slice(0, 5).map((j) => (
                <li key={j.job_id}>
                  <button type="button" className="link-row" onClick={() => router.push(`/jobs/${j.job_id}`)}>
                    <span className="link-row-main">
                      <StatusChip status={j.status} stage={j.stage} />
                      <span className="mono-sm">{j.job_id}</span>
                    </span>
                    <span className="link-row-side">{j.progress}%</span>
                  </button>
                  <ProgressBar value={j.progress} />
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      <section className="card" aria-label="Job terbaru">
        <div className="card-head row">
          <div>
            <h2>Terbaru</h2>
            <p>5 job terakhir. Lihat semua di halaman Jobs.</p>
          </div>
          <button type="button" className="btn btn-text btn-sm" onClick={() => router.push("/jobs")}>
            Semua jobs
          </button>
        </div>
        {loading ? (
          <p className="muted">Memuat…</p>
        ) : jobs.length === 0 ? (
          <p className="muted">Belum ada job. Buat project pertama di atas.</p>
        ) : (
          <ul className="mini-list">
            {jobs.slice(0, 5).map((j) => (
              <li key={j.job_id}>
                <button type="button" className="link-row" onClick={() => router.push(`/jobs/${j.job_id}`)}>
                  <span className="link-row-main">
                    <StatusChip status={j.status} stage={j.stage} />
                    <span className="ellipsis" title={j.video?.title ?? j.source_url}>{j.video?.title ?? j.source_url}</span>
                  </span>
                  <span className="link-row-side mono-sm">{j.clips?.length ?? 0} clip</span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

    </div>
  );
}
