"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { WorkflowGraph, taskState } from "../../components/graph";
import { Collapsible, ConfirmDialog, EmptyState, Icon, PageHeader, ProgressBar, StatusChip } from "../../components/ui";
import { cancelJob, clipUrl, deleteJob, formatBytes, formatClock, formatDuration, formatPublished, formatTime, getWorkerStatus, recreateJob, recreatePreview, refreshJobMetadata } from "../../lib/api";
import type { RecreatePreview, WorkerStatus } from "../../lib/api";
import { useJob } from "../../lib/hooks";
import { useSettings, useToast } from "../../lib/store";
import { TERMINAL_STATES } from "../../lib/types";

function QueueHint({ apiBase, jobId, status }: { apiBase: string; jobId: string; status: string }) {
  const [info, setInfo] = useState<WorkerStatus | null>(null);
  useEffect(() => {
    if (status !== "queued" && status !== "accepted") return;
    let cancelled = false;
    getWorkerStatus(apiBase)
      .then((res) => {
        if (!cancelled) setInfo(res);
      })
      .catch(() => {
        if (!cancelled) setInfo(null);
      });
    return () => {
      cancelled = true;
    };
  }, [apiBase, jobId, status]);
  if (status !== "queued" && status !== "accepted") return null;
  if (!info) return null;
  if (!info.worker_up) {
    return <p className="muted job-stage">Worker tidak merespons{info.note ? ` (${info.note})` : ""} — antrean macet, restart worker bila perlu.</p>;
  }
  const others = [...(info.active_jobs ?? []), ...(info.reserved_jobs ?? [])].filter((id) => id !== jobId);
  if (others.length === 0) return null;
  return <p className="muted job-stage">Menunggu worker · {others.length} job lain berjalan: {others.slice(0, 2).join(", ")}{others.length > 2 ? "…" : ""}</p>;
}

export default function JobDetailPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const { apiBase } = useSettings();
  const push = useToast();
  const { job, loading, error } = useJob(params.id);

  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [aiOpen, setAiOpen] = useState(false);
  const [confirm, setConfirm] = useState<"cancel" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [recreateOpen, setRecreateOpen] = useState(false);
  const [recreateInfo, setRecreateInfo] = useState<RecreatePreview | null>(null);
  const [recreating, setRecreating] = useState(false);
  const [forceRe, setForceRe] = useState(false);

  const onRefreshMeta = async () => {
    if (!job) return;
    setRefreshing(true);
    try {
      await refreshJobMetadata(apiBase, job.job_id);
      push("Refresh metadata diantrekan", "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal refresh", "error");
    } finally {
      setRefreshing(false);
    }
  };

  const toggle = (id: string) => setExpanded((c) => ({ ...c, [id]: !c[id] }));
  const focusTask = (clipId: string) => {
    setExpanded((c) => ({ ...c, [clipId]: true }));
    requestAnimationFrame(() => {
      document.getElementById(`task-${clipId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  };

  const doAction = async () => {
    if (!job || !confirm) return;
    setBusy(true);
    try {
      if (confirm === "cancel") {
        await cancelJob(apiBase, job.job_id);
        push("Job dibatalkan", "success");
      } else {
        await deleteJob(apiBase, job.job_id);
        push("Job dihapus", "success");
        router.push("/jobs");
        return;
      }
    } catch (e) {
      push(e instanceof Error ? e.message : "Aksi gagal", "error");
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const openRecreate = async () => {
    if (!job) return;
    setRecreateOpen(true);
    setRecreateInfo(null);
    try {
      setRecreateInfo(await recreatePreview(apiBase, job.job_id));
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal memuat info recreate", "error");
    }
  };

  const doRecreate = async () => {
    if (!job) return;
    setRecreating(true);
    try {
      const created = await recreateJob(apiBase, job.job_id, forceRe);
      push(
        created.job_id === job.job_id
          ? "Recreate sudah berjalan untuk job ini"
          : forceRe
            ? "Job recreate dibuat. Transkrip akan dibuat ulang."
            : "Job recreate dibuat. Menggunakan transkrip tersimpan.",
        "success",
      );
      setRecreateOpen(false);
      if (created.job_id !== job.job_id) router.push(`/jobs/${created.job_id}`);
    } catch (e) {
      push(e instanceof Error ? e.message : "Recreate gagal", "error");
    } finally {
      setRecreating(false);
    }
  };

  if (loading) {
    return (
      <div className="page">
        <PageHeader eyebrow="JOB DETAIL" title="Memuat…" />
        <p className="muted">Mengambil data job…</p>
      </div>
    );
  }

  if (error || !job) {
    return (
      <div className="page">
        <PageHeader eyebrow="JOB DETAIL" title="Tidak ditemukan" />
        <div className="card">
          <EmptyState icon="alert" title="Job tidak dapat dimuat" hint={error ?? "ID tidak dikenal."} action={
            <button type="button" className="btn btn-tonal btn-sm" onClick={() => router.push("/jobs")}>
              Kembali ke Jobs
            </button>
          } />
        </div>
      </div>
    );
  }

  const terminal = TERMINAL_STATES.includes(job.status);
  const curationInfo = (() => {
    const cur = job.curation;
    if (!cur) return null;
    if (cur.curation_method === "ai" || cur.source === "gemini") {
      return {
        label: `AI Curated${cur.model ? ` · ${cur.model}` : ""}`,
        tip: "Dipilih memakai analisis Gemini.",
      };
    }
    if (cur.curation_method === "heuristic") {
      return {
        label: "Fallback · Transcript Heuristic",
        tip: "Dipilih dari sinyal konten transkrip karena AI dan heatmap penonton tidak tersedia.",
      };
    }
    if (cur.curation_method === "heatmap" || cur.heatmap_used) {
      return {
        label: "Fallback · YouTube Heatmap",
        tip: "Dipilih dari data replay penonton YouTube karena kurasi AI tidak tersedia.",
      };
    }
    return {
      label: "Fallback · Deterministik",
      tip: "Dipilih dari timing transkrip karena tidak ada data skoring lain yang tersedia.",
    };
  })();
  const curationBadge = curationInfo?.label ?? null;
  const curationTip = curationInfo?.tip ?? null;
  const doneTasks = job.clip_tasks?.filter((t) => t.status === "done").length ?? 0;
  const clipCount = job.clips?.length ?? 0;
  const aiOk = job.curation?.source === "gemini" || job.curation?.curation_method === "ai";
  const aiProvider = job.curation?.provider_name || job.curation?.provider_id;
  const aiAttempts = job.curation?.attempts_total;
  const aiSummary = !job.curation
    ? { tone: "muted" as const, icon: "circle" as const, text: "Menunggu kurasi" }
    : aiOk
      ? { tone: "ok" as const, icon: "check" as const, text: `Gemini${job.curation?.model ? ` · ${job.curation.model}` : ""}${aiProvider ? ` · ${aiProvider}` : ""}${typeof aiAttempts === "number" ? ` · ${aiAttempts} upaya` : ""}` }
      : { tone: "warn" as const, icon: "alert" as const, text: "Gemini unavailable — fallback transcript" };

  return (
    <div className="page job-detail">
      <PageHeader
        eyebrow="JOB DETAIL"
        title={job.video?.title || job.job_id}
        desc={job.video?.title ? job.job_id : job.source_url}
        actions={
          <>
            <button type="button" className="btn btn-tonal btn-sm" onClick={openRecreate} title="Buat job baru dari transkrip tersimpan">
              <Icon name="refresh" size={14} /> Recreate
            </button>
            {!terminal && (
              <button type="button" className="btn btn-text btn-sm" onClick={() => setConfirm("cancel")}>
                Batalkan
              </button>
            )}
            {terminal && (
              <button type="button" className="btn btn-tonal btn-sm btn-danger" onClick={() => setConfirm("delete")}>
                <Icon name="trash" size={15} /> Hapus
              </button>
            )}
          </>
        }
      />

      {/* ── 1. Job header + summary (full width, compact) ── */}
      <section className="card job-head" aria-label="Video sumber">
        <div className="job-head-main">
          {job.video?.thumbnail_url ? (
            <img src={job.video.thumbnail_url} alt="" loading="lazy" className="job-head-thumb" />
          ) : (
            <span className="job-head-thumb job-head-empty" aria-hidden="true">
              <Icon name="image" size={22} />
            </span>
          )}
          <div className="job-head-info">
            <p className="job-channel muted">{job.video?.channel_name ?? "Channel unavailable"}</p>
            <p className="mono-sm ellipsis">
              <a href={job.source_url} target="_blank" rel="noreferrer">
                {job.source_url}
              </a>
            </p>
            <div className="job-head-status">
              <StatusChip status={job.status} stage={job.stage} />
              <span className="muted mono-sm">
                {job.progress}%{job.total_clips && job.total_clips >= 1 && job.total_clips <= 10 ? ` · Clip ${Math.min(job.current_clip ?? 0, job.total_clips)}/${job.total_clips}` : ""}
              </span>
            </div>
              <ProgressBar value={job.progress} />
              {job.stage_detail && <p className="muted job-stage">{job.stage_detail}</p>}
              <QueueHint apiBase={apiBase} jobId={job.job_id} status={job.status} />
          </div>
        </div>
        <ul className="job-summary">
          <li>
            <span>Durasi</span>
            <strong>{formatDuration(job.video?.duration) ?? "—"}</strong>
          </li>
          <li>
            <span>Publikasi</span>
            <strong>{formatPublished(job.video?.published_at)}</strong>
          </li>
          <li>
            <span>Hasil</span>
            <strong>{clipCount > 0 ? `${clipCount} clip` : terminal ? "Tidak ada clip" : "Merender…"}</strong>
          </li>
          <li>
            <span>Kurasi</span>
            <strong className="ellipsis" title={curationTip ?? undefined}>{curationBadge ?? "—"}</strong>
          </li>
        </ul>
        {!job.video?.title && (
          <button type="button" className="btn btn-tonal btn-sm job-refresh" onClick={onRefreshMeta} disabled={refreshing}>
            <Icon name="refresh" size={14} /> {refreshing ? "Mengantre…" : "Refresh Metadata"}
          </button>
        )}
        {job.error && (
          <div className="banner banner-error" role="alert">
            <Icon name="alert" size={16} />
            <span>
              {job.error.code ? `${job.error.code}: ` : ""}
              {job.error.message ?? "Job gagal diproses"}
            </span>
          </div>
        )}
      </section>

      {/* ── 2. Generated clips — fokus utama ── */}
      <section className="card clips-card" aria-label="Generated clips">
        <div className="card-head row">
          <div>
            <h2>Generated Clips <span className="muted">· {clipCount}</span></h2>
            <p>Potongan audio &amp; subtitle.</p>
          </div>
          {curationBadge && (
            <span className="chip chip-muted" title={curationTip ?? undefined}>{curationBadge}</span>
          )}
        </div>
        {!job.clips || job.clips.length === 0 ? (
          <p className="muted">{terminal ? "Tidak ada clip yang berhasil." : "Clip akan muncul saat render selesai."}</p>
        ) : (
          <div className="clip-grid">
            {job.clips.map((clip, i) => {
              const clock = formatClock(clip.duration_seconds);
              const size = formatBytes(clip.size_bytes);
              return (
                <article key={clip.clip_id} className="clip-card">
                  <div className="clip-media">
                    <video
                      preload="metadata"
                      src={clipUrl(apiBase, clip.download_url)}
                      controls
                      playsInline
                      className="clip-video"
                    />
                    {clock && <span className="clip-dur" aria-label={`Durasi ${clock}`}>{clock}</span>}
                  </div>
                  <div className="clip-body">
                    <strong className="clip-title" title={clip.title || clip.clip_id}>
                      {String(i + 1).padStart(2, "0")} · {clip.title || clip.clip_id}
                    </strong>
                    <p className="clip-meta muted">
                      {clock ?? "—"}{size ? ` • ${size}` : ""}
                      {typeof clip.score === "number" && (
                        <span className="clip-score"> · <Icon name="star" size={11} /> {clip.score.toFixed(2)}</span>
                      )}
                    </p>
                    <a className="btn btn-tonal btn-sm clip-dl" href={clipUrl(apiBase, clip.download_url)} download>
                      <Icon name="download" size={14} /> Unduh
                    </a>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* ── 3. Processing details (collapsed by default saat selesai) ── */}
      <Collapsible
        title="Processing Details"
        subtitle="Status, subtitle, AI processing"
        defaultOpen={!terminal || !!job.error}
        badge={<StatusChip status={job.status} stage={job.stage} />}
      >
        <ul className="kv">
          <li>
            <span>Bahasa</span>
            <span>{job.language}</span>
          </li>
          <li>
            <span>Dibuat</span>
            <span>{formatTime(job.created_at)}</span>
          </li>
          <li>
            <span>Diubah</span>
            <span>{formatTime(job.updated_at)}</span>
          </li>
          <li>
            <span>Subtitle</span>
            <span>
              {job.burn_subtitles === false
                ? "Tidak dibakar"
                : `Dibakar · ${job.subtitle_style ?? "casual_center"} · ${job.subtitle_position ?? "bottom_center"}`}
            </span>
          </li>
          <li>
            <span>Transkrip</span>
            <span>
              {job.transcript_reused
                ? `Cache digunakan${job.transcript_cache_id ? ` · ${job.transcript_cache_id}` : ""}`
                : "Baru di job ini"}
            </span>
          </li>
          {job.recreated_from_job_id && (
            <li>
              <span>Dibuat dari</span>
              <span>
                <a href={`/jobs/${job.recreated_from_job_id}`}>{job.recreated_from_job_id}</a>
              </span>
            </li>
          )}
        </ul>
        <div className={`ai-note ai-${aiSummary.tone}`}>
          <span className="ai-note-main"><Icon name={aiSummary.icon} size={15} /> {aiSummary.text}</span>
          {(job.curation?.fallback_reason || job.curation?.attempted_provider) && (
            <button type="button" className="btn btn-text btn-sm" onClick={() => setAiOpen((o) => !o)} aria-expanded={aiOpen}>
              {aiOpen ? "Sembunyikan" : "Details"}
            </button>
          )}
        </div>
        {aiOpen && (job.curation?.fallback_reason || job.curation?.attempted_provider) && (
          <p className="muted ai-detail">
            {job.curation?.attempted_provider ? `Provider: ${job.curation.attempted_provider}. ` : ""}
            {job.curation?.fallback_reason ?? ""}
          </p>
        )}
        {aiOpen && job.curation?.fallback_chain && job.curation.fallback_chain.length > 0 && (
          <ul className="kv">
            {job.curation.fallback_chain.map((entry, index) => (
              <li key={index}>
                <span>{entry.level ?? "level"}</span>
                <span>
                  {entry.status}
                  {entry.reason ? ` — ${entry.reason}` : ""}
                  {typeof entry.candidates === "number" ? ` (${entry.candidates} kandidat)` : ""}
                  {typeof entry.best_score === "number" ? ` · skor ${entry.best_score}` : ""}
                </span>
              </li>
            ))}
            {typeof job.curation?.best_score === "number" && (
              <li>
                <span>Skor terbaik</span>
                <span>{job.curation.best_score} · {job.curation.candidate_count ?? 0} kandidat dinilai{typeof job.curation?.runtime_ms === "number" ? ` · ${job.curation.runtime_ms} ms` : ""}</span>
              </li>
            )}
            {job.curation?.algorithm_version && (
              <li>
                <span>Algoritma</span>
                <span className="mono-sm">{job.curation.algorithm_version}</span>
              </li>
            )}
          </ul>
        )}
      </Collapsible>

      {/* ── 4. Workflow (collapsed default) ── */}
      <Collapsible
        title="Workflow"
        subtitle="AI processing pipeline"
        defaultOpen={!terminal}
        badge={job.clip_tasks && job.clip_tasks.length > 0 ? (
          <span className="chip">{doneTasks}/{job.clip_tasks.length} berhasil</span>
        ) : undefined}
      >
        <p className="muted coll-hint">Alur n8n-style. Klik node clip untuk ke detail, klik node AI/fallback untuk alasan.</p>
        <div className="ng-scroll">
          <WorkflowGraph job={job} onClipClick={focusTask} />
        </div>
      </Collapsible>

      {/* ── 5. Task details (collapsed default) ── */}
      <Collapsible
        title={`Task Details · ${doneTasks}/${job.clip_tasks?.length ?? 0} completed`}
        subtitle="Rincian teknis tiap clip"
        defaultOpen={false}
        badge={job.clip_tasks && job.clip_tasks.length > 0 ? (
          <span className="chip">{doneTasks}/{job.clip_tasks.length} berhasil</span>
        ) : undefined}
      >
        {!job.clip_tasks || job.clip_tasks.length === 0 ? (
          <EmptyState
            icon="clips"
            title="Belum ada rincian task"
            hint="Rincian tersedia untuk job yang dibuat setelah fitur roadmap. Job lama hanya menampilkan clip."
          />
        ) : (
          <div className="task-list">
            {job.clip_tasks.map((task) => {
              const state = taskState(task.status);
              const preview = job.clips?.find((c) => c.clip_id === task.clip_id);
              const open = !!expanded[task.clip_id];
              return (
                <article key={task.clip_id} id={`task-${task.clip_id}`} className={`task task-${state}`}>
                  <button type="button" className="task-head" onClick={() => toggle(task.clip_id)} aria-expanded={open}>
                    <span className={`task-icon task-icon-${state}`} aria-hidden="true">
                      <Icon name={state === "done" ? "check" : state === "failed" ? "close" : state === "active" ? "loader" : "circle"} size={13} />
                    </span>
                    <span className="task-title">
                      <strong className="ellipsis">{task.title || task.clip_id}</strong>
                      <small className="ellipsis">
                        {task.clip_id}
                        {task.stage_detail ? ` · ${task.stage_detail}` : ""}
                      </small>
                    </span>
                    <span className="chip-row">
                      {typeof task.score === "number" && (
                        <span className="mini-chip"><Icon name="star" size={11} /> {task.score.toFixed(2)}</span>
                      )}
                      {formatClock(task.duration_seconds ?? preview?.duration_seconds) && (
                        <span className="mini-chip">{formatClock(task.duration_seconds ?? preview?.duration_seconds)}</span>
                      )}
                      {formatBytes(task.size_bytes ?? preview?.size_bytes) && (
                        <span className="mini-chip">{formatBytes(task.size_bytes ?? preview?.size_bytes)}</span>
                      )}
                      <span className={`mini-chip mini-chip-${state}`}>
                        {state === "done" ? "sukses" : state === "failed" ? "gagal" : state === "active" ? "jalan" : "antre"}
                      </span>
                    </span>
                  </button>
                  {open && (
                    <div className="task-body">
                      {task.reason && <p className="muted">{task.reason}</p>}
                      {task.error?.message && (
                        <div className="banner banner-error" role="alert">
                          {task.error.code ? `${task.error.code}: ` : ""}
                          {task.error.message}
                        </div>
                      )}
                      {preview ? (
                        <a className="btn btn-tonal btn-sm" href={clipUrl(apiBase, preview.download_url)} download>
                          <Icon name="download" size={14} /> Unduh {task.clip_id}.mp4
                        </a>
                      ) : (
                        state === "failed" && <small className="muted">Tidak ada file — clip gagal sebelum render.</small>
                      )}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </Collapsible>

      {recreateOpen && (
        <div className="dialog-scrim" onClick={() => !recreating && setRecreateOpen(false)} role="presentation">
          <div className="dialog" role="alertdialog" aria-modal="true" aria-label="Recreate job" onClick={(e) => e.stopPropagation()}>
            <h3 className="dialog-title">Recreate Job</h3>
            <div className="dialog-msg">
              <p>Job baru akan dibuat menggunakan source dan transkrip yang sudah tersedia. Proses langsung dilanjutkan ke kurasi.</p>
              <ul className="kv" style={{ marginTop: 12 }}>
                <li>
                  <span>Source</span>
                  <span className="ellipsis">{recreateInfo?.video?.title ?? job?.source_url ?? "…"}</span>
                </li>
                <li>
                  <span>Transkrip</span>
                  <span>
                    {recreateInfo == null
                      ? "Memeriksa…"
                      : recreateInfo.transcript_available
                        ? `✓ Tersedia${recreateInfo.cache_hit ? " (cache)" : ""}`
                        : "Tidak ditemukan — perlu transkripsi ulang"}
                  </span>
                </li>
                <li>
                  <span>Whisper</span>
                  <span>{forceRe ? "Akan dijalankan ulang" : "↪ Akan dilewati"}</span>
                </li>
                <li>
                  <span>Kurasi</span>
                  <span>✓ Akan dijalankan ulang</span>
                </li>
              </ul>
              {!terminal && (
                <p className="muted" style={{ marginTop: 8 }}>
                  Job ini masih berjalan — recreate tetap dibuat sebagai job baru yang terpisah.
                </p>
              )}
              <label style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 12 }}>
                <input type="checkbox" checked={forceRe} onChange={(e) => setForceRe(e.target.checked)} disabled={recreating} />
                <span>Transkripsi ulang (abaikan cache)</span>
              </label>
            </div>
            <div className="dialog-actions">
              <button type="button" className="btn btn-text" onClick={() => setRecreateOpen(false)} disabled={recreating}>
                Batal
              </button>
              <button type="button" className="btn btn-filled" onClick={doRecreate} disabled={recreating} autoFocus>
                {recreating ? "Memproses…" : "Recreate"}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirm !== null}
        title={confirm === "cancel" ? "Batalkan job?" : "Hapus job?"}
        message={confirm === "cancel" ? "Job akan dihentikan." : "Job dan file-nya akan dihapus permanen."}
        confirmLabel={confirm === "cancel" ? "Ya, batalkan" : "Ya, hapus"}
        danger={confirm === "delete"}
        busy={busy}
        onConfirm={doAction}
        onClose={() => !busy && setConfirm(null)}
      />
    </div>
  );
}
