"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Icon, PageHeader, ProgressBar, Stat, StatusChip } from "./components/ui";
import { checkHealth, createJob, getAppSettings } from "./lib/api";
import { useJobs } from "./lib/hooks";
import { useSettings, useToast } from "./lib/store";
import { ACTIVE_STATES, SUBTITLE_POSITIONS, SUBTITLE_STYLES } from "./lib/types";
import type { SubtitlePosition, SubtitleStyle } from "./lib/types";

import { resolveSubtitlePreview } from "./lib/subtitles";

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
      const job = await createJob(apiBase, {
        source_url: url,
        language,
        max_clips: maxClips,
        min_duration: minDuration,
        max_duration: maxDuration,
        burn_subtitles: burnSubs,
        subtitle_style: subStyle,
        subtitle_position: subPos,
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
                </div>
                <SubtitlePreview style={subStyle} position={subPos} />
              </div>
            )}
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
