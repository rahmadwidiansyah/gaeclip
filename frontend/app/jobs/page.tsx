"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ConfirmDialog, EmptyState, Icon, PageHeader, Pagination, ProgressBar, StatusChip } from "../components/ui";
import {
  backfillVideos,
  cancelJob,
  deleteJob,
  formatDuration,
  formatTime,
  listChannels,
  recreateJob,
  refreshJobMetadata,
  searchJobs,
} from "../lib/api";
import { useSettings, useToast } from "../lib/store";
import { TERMINAL_STATES } from "../lib/types";
import type { ChannelInfo, Job } from "../lib/types";

type GroupBy = "none" | "channel" | "video" | "status" | "date";
type DatePreset = "all" | "today" | "7d" | "30d";

const SORTS = [
  { id: "newest", label: "Terbaru" },
  { id: "oldest", label: "Terlama" },
  { id: "updated", label: "Terakhir diubah" },
  { id: "title_az", label: "Judul A–Z" },
  { id: "title_za", label: "Judul Z–A" },
];

const STATUS_OPTIONS = ["all", "processing", "completed", "partial", "failed", "cancelled"];

function dateRange(preset: DatePreset): { from: number; to: number } {
  const now = Date.now() / 1000;
  if (preset === "today") {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return { from: d.getTime() / 1000, to: 0 };
  }
  if (preset === "7d") return { from: now - 7 * 86400, to: 0 };
  if (preset === "30d") return { from: now - 30 * 86400, to: 0 };
  return { from: 0, to: 0 };
}

function groupKeyOf(job: Job, groupBy: GroupBy): string {
  const video = job.video;
  if (groupBy === "channel") return video?.channel_name || "Tanpa channel";
  if (groupBy === "video") return video?.title || job.video_id || job.job_id;
  if (groupBy === "status") return job.status;
  if (groupBy === "date") {
    const d = new Date((job.created_at || 0) * 1000);
    return isNaN(d.getTime()) ? "Tanggal tak dikenal" : d.toLocaleDateString("id-ID", { day: "2-digit", month: "short", year: "numeric" });
  }
  return "";
}

export default function JobsPage() {
  const router = useRouter();
  const { apiBase } = useSettings();
  const push = useToast();

  const [q, setQ] = useState("");
  const [debQ, setDebQ] = useState("");
  const [status, setStatus] = useState("all");
  const [channel, setChannel] = useState("");
  const [datePreset, setDatePreset] = useState<DatePreset>("all");
  const [sort, setSort] = useState("newest");
  const [groupBy, setGroupBy] = useState<GroupBy>("none");
  const [perPage, setPerPage] = useState(20);
  const [page, setPage] = useState(1);
  const [showFilters, setShowFilters] = useState(false);

  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [confirm, setConfirm] = useState<{ kind: "cancel" | "delete" | "backfill" | "recreate"; job?: Job } | null>(null);
  const [busy, setBusy] = useState(false);
  const [tick, setTick] = useState(0);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setDebQ(q.trim());
      setPage(1);
    }, 400);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [q]);

  const grouped = groupBy !== "none";
  const effectivePerPage = grouped ? 200 : perPage;
  const effectivePage = grouped ? 1 : page;
  const totalPages = Math.max(1, Math.ceil(total / effectivePerPage));

  const fetchJobs = useCallback(async () => {
    setLoading(true);
    try {
      const range = dateRange(datePreset);
      const res = await searchJobs(apiBase, {
        q: debQ,
        status,
        channel,
        date_from: range.from,
        date_to: range.to,
        sort,
        page: effectivePage,
        per_page: effectivePerPage,
      });
      setJobs(res.jobs);
      setTotal(res.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Gagal memuat job");
    } finally {
      setLoading(false);
    }
  }, [apiBase, debQ, status, channel, datePreset, sort, effectivePage, effectivePerPage]);

  useEffect(() => {
    fetchJobs();
  }, [fetchJobs]);

  useEffect(() => {
    listChannels(apiBase)
      .then(setChannels)
      .catch(() => {});
  }, [apiBase, tick]);

  const groups = useMemo(() => {
    if (!grouped) return null;
    const map = new Map<string, Job[]>();
    for (const job of jobs) {
      const key = groupKeyOf(job, groupBy);
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(job);
    }
    return [...map.entries()];
  }, [jobs, grouped, groupBy]);

  const resetPage = () => setPage(1);

  const doConfirm = async () => {
    if (!confirm) return;
    setBusy(true);
    try {
      if (confirm.kind === "cancel" && confirm.job) {
        await cancelJob(apiBase, confirm.job.job_id);
        push("Job dibatalkan", "success");
      } else if (confirm.kind === "delete" && confirm.job) {
        await deleteJob(apiBase, confirm.job.job_id);
        push("Job dihapus", "success");
      } else if (confirm.kind === "recreate" && confirm.job) {
        const created = await recreateJob(apiBase, confirm.job.job_id);
        push("Job recreate dibuat. Menggunakan transkrip tersimpan.", "success");
        setConfirm(null);
        setBusy(false);
        router.push(`/jobs/${created.job_id}`);
        return;
      } else if (confirm.kind === "backfill") {
        const res = await backfillVideos(apiBase, 20);
        push(`Backfill: ${res.queued} antre, ${res.filled_from_cache} dari cache`, "success");
      }
      setTick((t) => t + 1);
      fetchJobs();
    } catch (e) {
      push(e instanceof Error ? e.message : "Aksi gagal", "error");
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const onRefreshMeta = async (job: Job) => {
    try {
      await refreshJobMetadata(apiBase, job.job_id);
      push("Refresh metadata diantrekan", "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal refresh", "error");
    }
  };

  const copyId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
      push("Job ID disalin", "info");
    } catch {
      push(id, "info");
    }
  };

  const hasActiveFilter = status !== "all" || channel !== "" || datePreset !== "all";

  const renderRow = (j: Job) => {
    const terminal = TERMINAL_STATES.includes(j.status);
    const title = j.video?.title;
    const missingMeta = !title;
    return (
      <tr key={j.job_id}>
        <td>
          <div className="job-cell">
            {j.video?.thumbnail_url ? (
              <img src={j.video.thumbnail_url} alt="" loading="lazy" className="job-thumb" />
            ) : (
              <span className="job-thumb job-thumb-empty" aria-hidden="true">
                <Icon name="image" size={16} />
              </span>
            )}
            <div className="job-cell-main">
              <div className="cell-main ellipsis" title={title ?? undefined}>
                {title ?? <span className="muted">Title unavailable</span>}
              </div>
              <div className="cell-sub ellipsis">
                {j.video?.channel_name ?? <span className="muted">Channel unavailable</span>}
                {formatDuration(j.video?.duration) ? ` · ${formatDuration(j.video?.duration)}` : ""}
              </div>
              <div className="cell-sub ellipsis mono-sm">
                {j.video?.youtube_video_id ?? j.video_id ?? ""} · {j.job_id}
              </div>
            </div>
          </div>
        </td>
        <td>
          <StatusChip status={j.status} stage={j.stage} />
        </td>
        <td className="cell-progress">
          <ProgressBar value={j.progress} />
          <span className="mono-sm">{j.progress}%</span>
        </td>
        <td className="cell-sub">{formatTime(j.created_at)}</td>
        <td className="th-right job-actions" onClick={(e) => e.stopPropagation()}>
          <button type="button" className="btn btn-text btn-sm" onClick={() => router.push(`/jobs/${j.job_id}`)} title="Buka detail">
            Buka
          </button>
          {missingMeta && (
            <button
              type="button"
              className="btn btn-table-action"
              onClick={() => onRefreshMeta(j)}
              title="Refresh metadata"
              aria-label={`Refresh metadata ${j.job_id}`}
            >
              <Icon name="refresh" size={16} />
            </button>
          )}
          <button
            type="button"
            className="btn btn-table-action"
            onClick={() => copyId(j.job_id)}
            title="Salin Job ID"
            aria-label="Salin Job ID"
          >
            <Icon name="copy" size={16} />
          </button>
          {!terminal ? (
            <button type="button" className="btn btn-text btn-sm" onClick={() => setConfirm({ kind: "cancel", job: j })} title="Batalkan job">
              Batal
            </button>
          ) : (
            <>
              <button
                type="button"
                className="btn btn-table-action btn-table-accent"
                onClick={() => setConfirm({ kind: "recreate", job: j })}
                title="Recreate — kurasi ulang dari transkrip tersimpan"
                aria-label={`Recreate ${j.job_id}`}
              >
                <Icon name="refresh" size={16} />
              </button>
              <button
                type="button"
                className="btn btn-table-action btn-table-danger"
                onClick={() => setConfirm({ kind: "delete", job: j })}
                title="Hapus job"
                aria-label={`Hapus ${j.job_id}`}
              >
                <Icon name="trash" size={16} />
              </button>
            </>
          )}
        </td>
      </tr>
    );
  };

  const renderTable = (rows: Job[]) => (
    <table className="table">
      <thead>
        <tr>
          <th>Video</th>
          <th>Status</th>
          <th>Progress</th>
          <th>Dibuat</th>
          <th className="th-right">Aksi</th>
        </tr>
      </thead>
      <tbody>{rows.map(renderRow)}</tbody>
    </table>
  );

  return (
    <div className="page">
      <PageHeader
        eyebrow="PROJECTS"
        title="Jobs"
        desc={`${total} job ditemukan. Klik Buka untuk detail.`}
        actions={
          <>
            <button type="button" className="btn btn-tonal btn-sm" onClick={() => setConfirm({ kind: "backfill" })} title="Isi metadata yang kosong">
              <Icon name="refresh" size={15} /> Backfill
            </button>
            <button type="button" className="btn btn-tonal btn-sm" onClick={() => { setTick((t) => t + 1); fetchJobs(); }}>
              <Icon name="refresh" size={15} /> Muat ulang
            </button>
          </>
        }
      />

      <div className="toolbar card">
        <div className="search">
          <Icon name="search" size={16} />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search videos, channels, job ID…"
            aria-label="Cari video, channel, job ID"
          />
          {q && (
            <button type="button" className="btn btn-icon btn-sm" onClick={() => setQ("")} aria-label="Bersihkan pencarian">
              <Icon name="close" size={14} />
            </button>
          )}
        </div>
        <button
          type="button"
          className={`btn btn-tonal btn-sm${hasActiveFilter ? " btn-filter-on" : ""}`}
          aria-expanded={showFilters}
          onClick={() => setShowFilters((s) => !s)}
        >
          <Icon name="filter" size={15} /> Filter{hasActiveFilter ? " •" : ""}
        </button>
        <select value={groupBy} onChange={(e) => setGroupBy(e.target.value as GroupBy)} aria-label="Group by" title="Group by">
          <option value="none">Group: off</option>
          <option value="channel">Channel</option>
          <option value="video">Video</option>
          <option value="status">Status</option>
          <option value="date">Tanggal</option>
        </select>
        <select value={sort} onChange={(e) => { setSort(e.target.value); resetPage(); }} aria-label="Sort" title="Sort">
          {SORTS.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
        {!grouped && (
          <select value={perPage} onChange={(e) => { setPerPage(Number(e.target.value)); resetPage(); }} aria-label="Per halaman" title="Per halaman">
            {[10, 20, 50, 100].map((n) => (
              <option key={n} value={n}>
                {n}/hal
              </option>
            ))}
          </select>
        )}
      </div>

      {showFilters && (
        <div className="card filter-panel" role="group" aria-label="Filter lanjutan">
          <div className="field-row-3">
            <div className="field">
              <label htmlFor="f-status">Status</label>
              <select id="f-status" value={status} onChange={(e) => { setStatus(e.target.value); resetPage(); }}>
                <option value="all">All</option>
                <option value="processing">Processing</option>
                <option value="completed">Completed</option>
                <option value="partial">Partial</option>
                <option value="failed">Failed</option>
                <option value="cancelled">Cancelled</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="f-channel">Channel</label>
              <select id="f-channel" value={channel} onChange={(e) => { setChannel(e.target.value); resetPage(); }}>
                <option value="">All Channels</option>
                {channels.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.name} ({c.jobs})
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label htmlFor="f-date">Tanggal</label>
              <select id="f-date" value={datePreset} onChange={(e) => { setDatePreset(e.target.value as typeof datePreset); resetPage(); }}>
                <option value="all">All</option>
                <option value="today">Today</option>
                <option value="7d">Last 7 days</option>
                <option value="30d">Last 30 days</option>
              </select>
            </div>
          </div>
          {hasActiveFilter && (
            <button
              type="button"
              className="btn btn-text btn-sm"
              onClick={() => { setStatus("all"); setChannel(""); setDatePreset("all"); resetPage(); }}
            >
              Bersihkan filter
            </button>
          )}
        </div>
      )}

      {error && (
        <div className="banner banner-error" role="alert">
          <Icon name="alert" size={16} /> {error}
        </div>
      )}

      <div className="card table-card">
        {loading ? (
          <p className="muted pad">Memuat…</p>
        ) : jobs.length === 0 ? (
          <EmptyState icon="jobs" title="No jobs found" hint="Ubah pencarian/filter atau buat project baru di Dashboard." />
        ) : grouped && groups ? (
          groups.map(([key, rows]) => {
            const shut = !!collapsed[key];
            return (
              <div key={key} className="group-block">
                <button
                  type="button"
                  className="group-head"
                  aria-expanded={!shut}
                  onClick={() => setCollapsed((c) => ({ ...c, [key]: !c[key] }))}
                >
                  <Icon name={shut ? "chevR" : "chevD"} size={16} />
                  <strong className="ellipsis">{key}</strong>
                  <span className="chip">{rows.length} job{rows.length > 1 ? "s" : ""}</span>
                </button>
                {!shut && renderTable(rows)}
              </div>
            );
          })
        ) : (
          renderTable(jobs)
        )}
        {!grouped && (
          <div className="table-foot">
            <span className="muted">{total} job</span>
            <Pagination page={Math.min(page, totalPages)} totalPages={totalPages} onPage={setPage} />
          </div>
        )}
      </div>

      <ConfirmDialog
        open={!!confirm}
        title={confirm?.kind === "cancel" ? "Batalkan job?" : confirm?.kind === "delete" ? "Hapus job?" : confirm?.kind === "recreate" ? "Recreate job?" : "Backfill metadata?"}
        message={
          confirm?.kind === "cancel"
            ? `Job ${confirm?.job?.job_id} akan dihentikan.`
            : confirm?.kind === "delete"
              ? `Job ${confirm?.job?.job_id} dan file-nya akan dihapus permanen.`
              : confirm?.kind === "recreate"
                ? `Job baru akan dibuat dari ${confirm?.job?.job_id} memakai transkrip tersimpan dan langsung kurasi ulang.`
                : "Isi metadata yang kosong dari cache/YouTube (maks 20 job, tanpa duplikat)."
        }
        confirmLabel={confirm?.kind === "backfill" ? "Ya, jalankan" : confirm?.kind === "cancel" ? "Ya, batalkan" : confirm?.kind === "recreate" ? "Ya, recreate" : "Ya, hapus"}
        danger={confirm?.kind === "delete"}
        busy={busy}
        onConfirm={doConfirm}
        onClose={() => !busy && setConfirm(null)}
      />
    </div>
  );
}
