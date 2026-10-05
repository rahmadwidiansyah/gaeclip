"use client";

import { useMemo, useState } from "react";
import { EmptyState, Icon, IconButton, PageHeader, Pagination, StatusChip } from "../components/ui";
import { ALL, distinctChannels, filterClips, flattenClips, sortClips, type ClipSort } from "../lib/clips";
import { clipUrl, formatBytes, formatClock } from "../lib/api";
import { useJobs } from "../lib/hooks";
import { useSettings } from "../lib/store";

const PAGE_SIZE = 24;

const SORTS: { id: ClipSort; label: string }[] = [
  { id: "new", label: "Terbaru" },
  { id: "old", label: "Terlama" },
  { id: "score", label: "Skor tertinggi" },
  { id: "duration_desc", label: "Durasi terpanjang" },
  { id: "duration_asc", label: "Durasi terpendek" },
];

export default function ClipsPage() {
  const { apiBase } = useSettings();
  const { jobs, loading, error, refresh } = useJobs(200);
  const [q, setQ] = useState("");
  const [channel, setChannel] = useState(ALL);
  const [jobFilter, setJobFilter] = useState(ALL);
  const [sort, setSort] = useState<ClipSort>("new");
  const [page, setPage] = useState(1);

  const allRows = useMemo(() => flattenClips(jobs), [jobs]);
  const channels = useMemo(() => distinctChannels(allRows), [allRows]);
  // Job options limited to jobs present in the loaded set.
  const jobOptions = useMemo(
    () => jobs.filter((j) => (j.clips?.length ?? 0) > 0).map((j) => j.job_id),
    [jobs]
  );

  const rows = useMemo(
    () => sortClips(filterClips(allRows, { q, channel, jobId: jobFilter }), sort),
    [allRows, q, channel, jobFilter, sort]
  );

  const resetPage = () => setPage(1);
  const hasFilter = q.trim() !== "" || channel !== ALL || jobFilter !== ALL;
  const clearFilters = () => {
    setQ("");
    setChannel(ALL);
    setJobFilter(ALL);
    setPage(1);
  };

  // Stale selections (e.g. after refresh) fall back to "all" instead of empty.
  const safeChannel = channels.some((c) => c.value === channel) ? channel : ALL;
  const safeJob = jobFilter === ALL || jobOptions.includes(jobFilter) ? jobFilter : ALL;

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const visible = rows.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  return (
    <div className="page page-wide clips-page">
      <PageHeader
        eyebrow="HASIL"
        title="Clips"
        desc={`${rows.length} clip · ${channels.length} channel · Pratinjau & unduh.`}
        actions={
          <>
            {hasFilter && (
              <button type="button" className="btn btn-text btn-sm" onClick={clearFilters}>
                <Icon name="close" size={14} /> Reset filter
              </button>
            )}
            <button type="button" className="btn btn-tonal btn-sm" onClick={refresh} aria-label="Muat ulang clips">
              <Icon name="refresh" size={15} /> Muat ulang
            </button>
          </>
        }
      />

      <div className="toolbar card clips-toolbar" role="search">
        <div className="search">
          <Icon name="search" size={16} />
          <input
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              resetPage();
            }}
            placeholder="Cari judul / id / channel…"
            aria-label="Cari clip"
          />
          {q && (
            <IconButton icon="close" label="Bersihkan pencarian" variant="ghost" onClick={() => { setQ(""); resetPage(); }} />
          )}
        </div>
        <select
          value={safeChannel}
          onChange={(e) => {
            setChannel(e.target.value);
            resetPage();
          }}
          aria-label="Filter channel"
          title="Filter channel"
        >
          <option value={ALL}>Semua channel</option>
          {channels.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label} ({c.count})
            </option>
          ))}
        </select>
        <select
          value={safeJob}
          onChange={(e) => {
            setJobFilter(e.target.value);
            resetPage();
          }}
          aria-label="Filter job"
          title="Filter job"
        >
          <option value={ALL}>Semua job</option>
          {jobOptions.map((id) => (
            <option key={id} value={id}>
              {id.slice(0, 18)}…
            </option>
          ))}
        </select>
        <select
          value={sort}
          onChange={(e) => {
            setSort(e.target.value as ClipSort);
            resetPage();
          }}
          aria-label="Urutkan"
          title="Urutkan"
        >
          {SORTS.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      {error && <p className="banner banner-error">{error}</p>}

      {loading ? (
        <p className="muted">Memuat…</p>
      ) : allRows.length === 0 ? (
        <div className="card">
          <EmptyState icon="clips" title="Belum ada clip" hint="Clip muncul setelah job selesai merender." />
        </div>
      ) : visible.length === 0 ? (
        <div className="card">
          <EmptyState
            icon="search"
            title="Tidak ada clip ditemukan"
            hint="Coba ubah channel, pencarian, atau filter job."
            action={
              <button type="button" className="btn btn-tonal btn-sm" onClick={clearFilters}>
                <Icon name="close" size={14} /> Reset filter
              </button>
            }
          />
        </div>
      ) : (
        <>
          <div className="clip-grid">
            {visible.map((c) => (
              <article key={`${c.jobId}-${c.clip_id}`} className="clip-card">
                <div className="clip-media">
                  <video preload="metadata" src={clipUrl(apiBase, c.download_url)} controls playsInline className="clip-video" />
                  {formatClock(c.duration_seconds) && <span className="clip-dur">{formatClock(c.duration_seconds)}</span>}
                </div>
                <div className="clip-body">
                  <strong className="clip-title-2" title={`${c.title || c.clip_id}${c.channel ? ` — ${c.channel}` : ""}`}>
                    {c.title || c.clip_id}
                  </strong>
                  {c.channel && (
                    <p className="clip-channel ellipsis" title={c.channel}>
                      {c.channel}
                    </p>
                  )}
                  <p className="clip-meta muted">
                    {formatClock(c.duration_seconds) ?? "—"}{formatBytes(c.size_bytes) ? ` • ${formatBytes(c.size_bytes)}` : ""}
                  </p>
                  <div className="clip-sub">
                    <StatusChip status={c.jobStatus} />
                    {typeof c.score === "number" && (
                      <span className="clip-score muted"><Icon name="star" size={11} /> {c.score.toFixed(2)}</span>
                    )}
                  </div>
                  <div className="clip-actions">
                    <a className="btn btn-tonal btn-sm" href={clipUrl(apiBase, c.download_url)} download aria-label={`Unduh ${c.title || c.clip_id}`}>
                      <Icon name="download" size={14} /> Unduh
                    </a>
                    <a className="btn btn-text btn-sm mono-sm ellipsis" href={`/jobs/${c.jobId}`} title={c.jobId}>
                      {c.jobId.slice(0, 12)}…
                    </a>
                  </div>
                </div>
              </article>
            ))}
          </div>
          <div className="table-foot">
            <span className="muted">{rows.length} clip · hal {safePage}/{totalPages}</span>
            <Pagination page={safePage} totalPages={totalPages} onPage={setPage} />
          </div>
        </>
      )}
    </div>
  );
}
