"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { Collapsible, ConfirmDialog, Icon, IconButton } from "../components/ui";
import { formatTime } from "../lib/api";
import {
  aggregatePublishStatus,
  cancelPublishJob,
  createPublish,
  determinateProgress,
  failureKind,
  formatHashtagsInput,
  generateMetadata,
  initialCaption,
  initialPublishValues,
  isActivePublishStatus,
  isTerminalPublishStatus,
  listPublishJobs,
  listSocialAccounts,
  listSocialProviders,
  localTimeZone,
  parseHashtagsInput,
  phaseMessage,
  refreshPublishJob,
  retryPublishJob,
  statusLabel,
  statusTone,
  summarizeJobs,
  timeZoneList,
  zonedLocalToEpoch,
  type ClipPublishSummary,
  type ClipPublishState,
  type MetadataPackage,
  type PublishInput,
  type SocialAccount,
  type SocialCapabilities,
  type SocialPublishJob,
} from "../lib/social";

const PROVIDER_LABEL: Record<string, string> = {
  youtube: "YouTube",
  tiktok: "TikTok",
  instagram: "Instagram",
  facebook: "Facebook",
};

export function useClipPublish(apiBase: string, clipId: string) {
  const [jobs, setJobs] = useState<SocialPublishJob[] | null>(null);
  const [summary, setSummary] = useState<ClipPublishSummary | null>(null);

  const reload = useCallback(async () => {
    try {
      const res = await listPublishJobs(apiBase, clipId);
      setJobs(res.jobs);
      setSummary(res.summary ?? summarizeJobs(res.jobs));
    } catch {
      /* history is best-effort; actions surface errors via toast */
    }
  }, [apiBase, clipId]);

  useEffect(() => {
    reload();
  }, [reload]);

  // Polling strategy (no websocket yet):
  // - uploading/preparing/reconciling: 4s (fast, bytes moving)
  // - processing/uploaded only: 15s (provider-side work, bounded backend)
  // - terminal: stop. Scheduled counts as active (waiting) but polls slowly.
  const statuses = (jobs ?? []).map((j) => j.status);
  const hasUploading = statuses.some((s) =>
    ["pending", "queued", "preparing", "authorizing", "uploading", "reconciling"].includes(s));
  const hasProcessing = statuses.some((s) => s === "processing" || s === "uploaded");
  const hasActive = statuses.some((s) => isActivePublishStatus(s));
  const interval = hasUploading ? 4000 : hasProcessing ? 15000 : hasActive ? 15000 : null;
  useEffect(() => {
    if (interval === null) return;
    const timer = window.setInterval(reload, interval);
    return () => window.clearInterval(timer);
  }, [interval, reload]);

  return { jobs, reload, summary, publishState: aggregatePublishStatus(jobs ?? []) };
}

export function PublishActionButton({
  publishState,
  busy,
  summary,
  onPublish,
  onRetry,
}: {
  publishState: ClipPublishState;
  busy: boolean;
  summary?: ClipPublishSummary | null;
  onPublish: () => void;
  onRetry: () => void;
}) {
  if (publishState === "active" || publishState === "publishing" || publishState === "queued") {
    return (
      <button type="button" className="btn btn-filled btn-sm" disabled title="Upload sedang berjalan">
        <Icon name="loader" size={14} /> Publishing…
      </button>
    );
  }
  if (publishState === "processing") {
    return (
      <button type="button" className="btn btn-tonal btn-sm" disabled title="Provider sedang memproses video">
        <Icon name="loader" size={14} /> Processing…
      </button>
    );
  }
  if (publishState === "scheduled") {
    return (
      <button type="button" className="btn btn-tonal btn-sm" disabled title="Menunggu jadwal publish">
        <Icon name="clock" size={14} /> Scheduled
      </button>
    );
  }
  if (publishState === "completed" || publishState === "published") {
    return (
      <button type="button" className="btn btn-tonal btn-sm" onClick={onPublish} title="Sudah terpublish — klik untuk publish ke platform lain">
        <Icon name="check" size={14} /> Published
      </button>
    );
  }
  if (publishState === "partial_success") {
    const done = summary?.completed ?? 0;
    const total = summary?.total ?? 0;
    return (
      <button type="button" className="btn btn-tonal btn-sm" onClick={onPublish} title={`${done}/${total} platform published — klik untuk retry yang gagal`}>
        <Icon name="alert" size={14} /> {total > 0 ? `${done}/${total} Published` : "Partial"}
      </button>
    );
  }
  if (publishState === "draft") {
    return (
      <button type="button" className="btn btn-tonal btn-sm" onClick={onPublish} title="Tersimpan sebagai draft — klik untuk publish">
        Draft
      </button>
    );
  }
  if (publishState === "failed") {
    return (
      <button type="button" className="btn btn-filled btn-sm" onClick={onRetry} disabled={busy} title="Ulangi publish yang gagal">
        <Icon name="alert" size={14} /> {busy ? "Memproses…" : "Retry"}
      </button>
    );
  }
  if (publishState === "cancelled") {
    return (
      <button type="button" className="btn btn-tonal btn-sm" onClick={onPublish} disabled={busy} title="Publish dibatalkan — klik untuk publish ulang">
        <Icon name="upload" size={14} /> Publish
      </button>
    );
  }
  return (
    <button type="button" className="btn btn-filled btn-sm" onClick={onPublish} disabled={busy} title="Publish clip ke social media">
      <Icon name="upload" size={14} /> Publish
    </button>
  );
}

export function PublishHistory({
  jobs,
  accountsById,
  busyId,
  summary,
  onRetry,
  onCancel,
  onRefresh,
}: {
  jobs: SocialPublishJob[] | null;
  accountsById: Record<string, string>;
  busyId: string | null;
  summary?: ClipPublishSummary | null;
  onRetry: (jobId: string) => void;
  onCancel: (jobId: string) => void;
  onRefresh?: (jobId: string) => void;
}) {
  if (!jobs || jobs.length === 0) return null;
  const sum = summary ?? summarizeJobs(jobs);
  const subtitle = sum.total <= 1
    ? `${PROVIDER_LABEL[jobs[0].provider] ?? jobs[0].provider} · ${statusLabel(jobs[0].status)}`
    : `${sum.completed}/${sum.total} published${sum.processing > 0 ? ` · ${sum.processing} processing` : ""}${sum.failed > 0 ? ` · ${sum.failed} failed` : ""}`;
  return (
    <Collapsible title="Riwayat publish" subtitle={subtitle} defaultOpen={false}>
      {sum.total > 1 && (
        <p className="muted" aria-live="polite">
          Publishing to {sum.total} platforms · {sum.completed} published
          {sum.processing > 0 ? ` · ${sum.processing} processing` : ""}
          {sum.failed > 0 ? ` · ${sum.failed} failed` : ""}
        </p>
      )}
      <ul className="pub-list">
        {jobs.map((job) => {
          const pct = determinateProgress(job);
          const providerUrl = job.provider_url || job.remote_url;
          const kind = failureKind(job.error_code);
          const hint = job.status === "failed"
            ? kind === "auth"
              ? "Account needs reconnect."
              : kind === "retryable"
                ? "Temporary issue — safe to retry."
                : kind === "processing"
                  ? "Provider could not process this video."
                  : kind === "invalid"
                    ? "Check title/privacy/file."
                    : job.error_message
            : "";
          return (
          <li key={job.id} className="pub-row">
            <span className="pub-main">
              <Icon name={job.provider as "youtube" | "tiktok" | "instagram" | "facebook"} size={15} />{" "}
              <strong>{PROVIDER_LABEL[job.provider] ?? job.provider}</strong>{" "}
              <span className={`chip chip-${statusTone(job.status)}`}>{statusLabel(job.status)}</span>
              {accountsById[job.social_account_id] && <small className="muted"> · {accountsById[job.social_account_id]}</small>}
              <small className="muted"> · {phaseMessage(job)}</small>
              {job.status === "uploading" && pct !== null && (
                <span className="pbar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={`Uploading to ${PROVIDER_LABEL[job.provider] ?? job.provider} ${pct}%`}>
                  <span className="pbar-fill" style={{ width: `${pct}%` }} />
                </span>
              )}
              {job.status === "uploading" && pct === null && (
                <small className="muted" role="status" aria-label={`Uploading to ${PROVIDER_LABEL[job.provider] ?? job.provider}`}>
                  {" "}●●●
                </small>
              )}
              {(job.status === "processing" || job.status === "uploaded" || job.status === "reconciling") && (
                <small className="muted" role="status" aria-label={phaseMessage(job)}>
                  {" "}◌
                </small>
              )}
              {job.created_at > 0 && (
                <small className="muted" title={new Date(job.created_at * 1000).toLocaleString()}>
                  {" "}
                  · {formatTime(job.created_at)}
                </small>
              )}
              {hint && <small className="muted"> — {hint}</small>}
              {job.status === "failed" && job.error_message && hint !== job.error_message && (
                <small className="muted" title={job.error_message}> — {job.error_message}</small>
              )}
              {providerUrl ? (
                <>
                  {" "}
                  <a href={providerUrl} target="_blank" rel="noopener noreferrer">
                    {job.provider === "youtube" ? "Open YouTube" : "Open"}
                  </a>
                </>
              ) : null}
            </span>
            <span className="row-actions">
              {(job.status === "processing" || job.status === "uploaded" || job.status === "reconciling") && onRefresh && (
                <button
                  type="button"
                  className="btn btn-text btn-sm"
                  disabled={busyId !== null}
                  onClick={() => onRefresh(job.id)}
                  title="Check provider status now"
                >
                  Refresh
                </button>
              )}
              {job.status === "failed" && (
                <button
                  type="button"
                  className="btn btn-tonal btn-sm"
                  disabled={busyId !== null}
                  onClick={() => onRetry(job.id)}
                  title={`Retry ${PROVIDER_LABEL[job.provider] ?? job.provider} only`}
                >
                  Retry
                </button>
              )}
              {!isTerminalPublishStatus(job.status) && (
                <button
                  type="button"
                  className="btn btn-text btn-sm"
                  disabled={busyId !== null}
                  onClick={() => onCancel(job.id)}
                >
                  Batal
                </button>
              )}
            </span>
          </li>
          );
        })}
      </ul>
    </Collapsible>
  );
}

export function ClipPublishZone({
  apiBase,
  jobId,
  clipId,
  clipTitle,
  clipDescription = "",
  clipHashtags = [],
  clipMetadataSource = null,
  push,
  children,
}: {
  apiBase: string;
  jobId: string;
  clipId: string;
  clipTitle: string;
  clipDescription?: string;
  clipHashtags?: string[];
  clipMetadataSource?: string | null;
  push: (message: string, kind?: "info" | "success" | "error") => void;
  children?: React.ReactNode;
}) {
  const { jobs, reload, summary, publishState } = useClipPublish(apiBase, clipId);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [accountsById, setAccountsById] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    listSocialAccounts(apiBase)
      .then((res) => {
        if (!alive) return;
        const map: Record<string, string> = {};
        for (const acc of res.accounts) map[acc.id] = acc.display_name || acc.external_account_id;
        setAccountsById(map);
      })
      .catch(() => {
        /* account names are enrichment-only; history works without them */
      });
    return () => {
      alive = false;
    };
  }, [apiBase]);

  const retryFailed = async () => {
    const failed = (jobs ?? []).filter((j) => j.status === "failed");
    if (failed.length === 0) return;
    setActionBusy(true);
    try {
      for (const job of failed) {
        await retryPublishJob(apiBase, job.id);
      }
      push(failed.length === 1 ? "Retry dimulai" : `Retry dimulai untuk ${failed.length} job`, "info");
      reload();
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal retry", "error");
    } finally {
      setActionBusy(false);
    }
  };

  const handleRetryOne = async (id: string) => {
    setBusyId(id);
    try {
      await retryPublishJob(apiBase, id);
      push("Retry dimulai", "info");
      reload();
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal retry", "error");
    } finally {
      setBusyId(null);
    }
  };

  const handleRefreshOne = async (id: string) => {
    setBusyId(id);
    try {
      await refreshPublishJob(apiBase, id);
      reload();
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal refresh", "error");
    } finally {
      setBusyId(null);
    }
  };

  const handleCancelOne = async (id: string) => {
    setBusyId(id);
    try {
      await cancelPublishJob(apiBase, id);
      push("Publish dibatalkan", "info");
      reload();
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal cancel", "error");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <div className="clip-actions">
        <PublishActionButton
          publishState={publishState}
          busy={actionBusy}
          summary={summary}
          onPublish={() => setDialogOpen(true)}
          onRetry={retryFailed}
        />
        {children}
      </div>
      <PublishDialog
        apiBase={apiBase}
        jobId={jobId}
        clipId={clipId}
        clipTitle={clipTitle}
        clipDescription={clipDescription}
        clipHashtags={clipHashtags}
        clipMetadataSource={clipMetadataSource}
        push={push}
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        onSubmitted={() => reload()}
      />
      <PublishHistory jobs={jobs} accountsById={accountsById} busyId={busyId} summary={summary} onRetry={handleRetryOne} onCancel={handleCancelOne} onRefresh={handleRefreshOne} />
    </>
  );
}

export function PublishDialog({
  apiBase,
  jobId,
  clipId,
  clipTitle,
  clipDescription = "",
  clipHashtags = [],
  clipMetadataSource = null,
  push,
  open,
  onClose,
  onSubmitted,
}: {
  apiBase: string;
  jobId: string;
  clipId: string;
  clipTitle: string;
  clipDescription?: string;
  clipHashtags?: string[];
  clipMetadataSource?: string | null;
  push: (message: string, kind?: "info" | "success" | "error") => void;
  open: boolean;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const [caps, setCaps] = useState<Record<string, SocialCapabilities> | null>(null);
  const [accounts, setAccounts] = useState<SocialAccount[]>([]);
  const [selected, setSelected] = useState<Record<string, boolean>>({ youtube: true });
  const [accountFor, setAccountFor] = useState<Record<string, string>>({});
  const [titleVal, setTitleVal] = useState(clipTitle);
  const [captionVal, setCaptionVal] = useState("");
  const [hashtagsVal, setHashtagsVal] = useState("");
  const [dirty, setDirty] = useState({ title: false, description: false, hashtags: false });
  const [aiBusy, setAiBusy] = useState<null | "all" | "title" | "description" | "hashtags">(null);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiSource, setAiSource] = useState<"gemini" | "deterministic_fallback" | null>(null);
  const [confirmScope, setConfirmScope] = useState<null | "all" | "title" | "description" | "hashtags">(null);
  const [privacy, setPrivacy] = useState("public");
  const [when, setWhen] = useState<"now" | "schedule">("now");
  const [schedDate, setSchedDate] = useState("");
  const [schedTime, setSchedTime] = useState("");
  const [timezone, setTimezone] = useState(() => localTimeZone());
  const [busy, setBusy] = useState(false);
  const aiAbort = useRef<AbortController | null>(null);

  // User edits mark fields dirty; AI fills clear the dirty flag.
  const editTitle = (v: string) => { setTitleVal(v); setDirty((d) => ({ ...d, title: true })); };
  const editCaption = (v: string) => { setCaptionVal(v); setDirty((d) => ({ ...d, description: true })); };
  const editHashtags = (v: string) => { setHashtagsVal(v); setDirty((d) => ({ ...d, hashtags: true })); };

  useEffect(() => {
    if (!open) return;
    // Prefill from metadata saved during clip creation (no LLM call here).
    // Legacy clips without stored metadata start blank, as before.
    const initial = initialPublishValues({
      description: clipDescription,
      hashtags: clipHashtags,
      metadata_source: clipMetadataSource,
    });
    setTitleVal(clipTitle);
    setCaptionVal(initial.caption);
    setHashtagsVal(initial.hashtags);
    setDirty({ title: false, description: false, hashtags: false });
    setAiError(null);
    setAiSource(initial.aiSource);
    setConfirmScope(null);
    setWhen("now");
    let alive = true;
    Promise.all([listSocialProviders(apiBase), listSocialAccounts(apiBase)])
      .then(([c, a]) => {
        if (!alive) return;
        setCaps(c.providers);
        setAccounts(a.accounts);
        const first: Record<string, string> = {};
        for (const acc of a.accounts) {
          if (acc.status === "connected" && !first[acc.provider]) first[acc.provider] = acc.id;
        }
        setAccountFor(first);
      })
      .catch((e) => push(e instanceof Error ? e.message : "Gagal memuat akun social", "error"));
    return () => {
      alive = false;
    };
  }, [open, apiBase, clipTitle, clipDescription, clipHashtags, clipMetadataSource, push]);

  // Live updates are owned by the zone's history polling; the dialog
  // only needs the terminal toast, which the history reflects on reload.

  const providers = caps ? Object.keys(caps) : [];
  const chosen = providers.filter((p) => selected[p]);
  const tagList = parseHashtagsInput(hashtagsVal);
  const scheduledEpoch = when === "schedule" ? zonedLocalToEpoch(schedDate, schedTime, timezone) : null;
  const anyPrivacy = !caps || chosen.some((p) => caps?.[p]?.privacy !== false);
  const allDraft = chosen.length > 0 && chosen.every((p) => {
    const c = caps?.[p];
    return c ? !c.direct_publish && c.draft_upload : p === "tiktok";
  });

  const issues: string[] = [];
  if (chosen.length === 0) issues.push("Pilih minimal 1 platform.");
  const unconnected = chosen.filter(
    (p) => (accounts.find((a) => a.id === accountFor[p] && a.provider === p)?.status ?? "") !== "connected",
  );
  if (unconnected.length > 0) {
    issues.push(`Pilih akun yang connected untuk: ${unconnected.map((p) => PROVIDER_LABEL[p] ?? p).join(", ")}.`);
  }
  if (when === "schedule" && !(scheduledEpoch !== null && scheduledEpoch > Date.now() / 1000)) {
    issues.push("Isi tanggal & jam di masa depan.");
  }
  const disableReason = issues[0];
  const canSubmit = !busy && !aiBusy && issues.length === 0;

  const requestClose = () => {
    if (busy || aiBusy) return;
    aiAbort.current?.abort();
    onClose();
  };

  type MetaScope = "all" | "title" | "description" | "hashtags";
  const doGenerate = async (scope: MetaScope) => {
    const fields = scope === "all" ? (["title", "description", "hashtags"] as const) : [scope];
    setAiBusy(scope);
    setAiError(null);
    const ctrl = new AbortController();
    aiAbort.current = ctrl;
    try {
      const res = await generateMetadata(
        apiBase,
        {
          clip_id: clipId,
          job_id: jobId,
          platforms: chosen,
          fields: [...fields],
          title_override: scope !== "title" && scope !== "all" ? (titleVal.trim() || null) : null,
          description_override: scope !== "description" && scope !== "all" ? (captionVal.trim() || null) : null,
          hashtags_override: scope !== "hashtags" && scope !== "all" ? parseHashtagsInput(hashtagsVal) : null,
        },
        { signal: ctrl.signal },
      );
      if (fields.includes("title")) setTitleVal(res.title);
      if (fields.includes("description")) setCaptionVal(res.description);
      if (fields.includes("hashtags")) setHashtagsVal(formatHashtagsInput(res.hashtags));
      // AI-filled fields are no longer "user-dirty".
      setDirty((d) => ({
        title: fields.includes("title") ? false : d.title,
        description: fields.includes("description") ? false : d.description,
        hashtags: fields.includes("hashtags") ? false : d.hashtags,
      }));
      setAiSource(res.source);
      push(res.source === "gemini" ? "Metadata dibuat dengan AI" : "AI tidak tersedia — memakai fallback lokal", res.source === "gemini" ? "success" : "info");
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setAiError(e instanceof Error ? e.message : "AI generation gagal. Konten lama dipertahankan.");
    } finally {
      if (aiAbort.current === ctrl) aiAbort.current = null;
      setAiBusy(null);
    }
  };

  const requestGenerate = (scope: MetaScope) => {
    if (aiBusy) return;
    const targets = scope === "all" ? (["title", "description", "hashtags"] as const) : [scope];
    const touched = targets.some((f) => dirty[f]);
    if (touched) {
      setConfirmScope(scope);
      return;
    }
    void doGenerate(scope);
  };

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    const created: string[] = [];
    try {
      for (const provider of chosen) {
        const input: PublishInput = {
          clip_id: clipId,
          job_id: jobId,
          provider,
          account_id: accountFor[provider],
          title: titleVal.trim() || clipTitle,
          caption: (captionVal.trim() || initialCaption(titleVal.trim() || clipTitle, [])) as string,
          hashtags: tagList.map((t) => t.replace(/^#/, "")),
          privacy,
          scheduled_at: when === "schedule" ? scheduledEpoch : null,
        };
        const job = await createPublish(apiBase, input);
        created.push(job.id);
      }
      onClose();
      onSubmitted();
      push(`Publish dimulai ke ${created.length} platform…`, "info");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal publish", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {open && (
        <div className="dialog-scrim" onClick={() => requestClose()} role="presentation">
          <div
            className="dialog dialog-wide"
            role="dialog"
            aria-modal="true"
            aria-label="Publish Clip"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="dialog-head">
              <h3 className="dialog-title">Publish Clip</h3>
              <IconButton icon="close" label="Tutup dialog" title="Tutup" onClick={requestClose} disabled={busy || aiBusy !== null} />
            </div>
            <p className="muted">Choose where and how this clip will be published.</p>
            <div className="dialog-body">
            <div className="field">
              <span id="pub-platforms">Platform</span>
              {providers.length === 0 && <p className="muted">Memuat provider…</p>}
              <div className="plat-grid" role="group" aria-labelledby="pub-platforms">
                {providers.map((provider) => {
                  const mine = accounts.filter((a) => a.provider === provider);
                  const connected = mine.filter((a) => a.status === "connected");
                  const pressed = !!selected[provider];
                  const status =
                    connected.length === 0
                      ? "Not connected"
                      : provider === "tiktok" && caps && !caps.tiktok.direct_publish
                        ? "Draft only"
                        : "Connected";
                  return (
                    <button
                      key={provider}
                      type="button"
                      className="plat-card"
                      aria-pressed={pressed}
                      aria-label={`${PROVIDER_LABEL[provider] ?? provider}: ${status}${pressed ? " (dipilih)" : ""}`}
                      title={`${PROVIDER_LABEL[provider] ?? provider} — ${status}`}
                      onClick={() => setSelected((s) => ({ ...s, [provider]: !s[provider] }))}
                    >
                      <span className="plat-check" aria-hidden="true">
                        <Icon name="check" size={12} />
                      </span>
                      <Icon name={provider as "youtube" | "tiktok" | "instagram" | "facebook"} size={22} />
                      <span className="plat-name">{PROVIDER_LABEL[provider] ?? provider}</span>
                      <span className="plat-status">{status}</span>
                    </button>
                  );
                })}
              </div>
              {chosen.map((provider) => {
                const connected = accounts.filter((a) => a.provider === provider && a.status === "connected");
                return (
                  <div key={provider} className="plat-detail">
                    <strong>{PROVIDER_LABEL[provider] ?? provider}</strong>
                    {connected.length > 0 ? (
                      <select
                        aria-label={`Akun ${provider}`}
                        value={accountFor[provider] ?? ""}
                        onChange={(e) => setAccountFor((m) => ({ ...m, [provider]: e.target.value }))}
                      >
                        {connected.map((a) => (
                          <option key={a.id} value={a.id}>
                            {a.display_name || a.external_account_id}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className="muted">
                        Belum ada akun connected —{" "}
                        <Link href="/settings">hubungkan di Settings → Social</Link>
                      </span>
                    )}
                  </div>
                );
              })}
              {selected.tiktok && caps?.tiktok && !caps.tiktok.direct_publish && (
                <p className="muted">TikTok: {caps.tiktok.approval_note || "tanpa direct post, upload jadi draft."}</p>
              )}
            </div>
            <div className="field">
              <div className="meta-head">
                <span id="pub-content">Content</span>
                <button
                  type="button"
                  className="btn btn-tonal btn-sm"
                  onClick={() => requestGenerate("all")}
                  disabled={aiBusy !== null || busy}
                  title="Generate title, description & hashtags dengan AI"
                >
                  <Icon name="refresh" size={14} /> {aiBusy === "all" ? "Generating…" : "Generate with AI"}
                </button>
              </div>
              <small className="muted">Review or generate the title, description, and hashtags for this clip.</small>
              {aiSource && (
                <small className="muted" aria-live="polite">
                  {aiSource === "gemini" ? "Generated with AI" : "Generated locally (AI unavailable)"}
                </small>
              )}
              {aiError && (
                <p className="field-err" role="alert">
                  {aiError}{" "}
                  <button type="button" className="btn btn-text btn-sm" onClick={() => requestGenerate("all")}>
                    Try again
                  </button>
                </p>
              )}
              <label htmlFor="pub-title">Title</label>
              <div className="meta-row">
                <input
                  id="pub-title"
                  value={titleVal}
                  maxLength={100}
                  onChange={(e) => editTitle(e.target.value)}
                  placeholder={clipTitle}
                  aria-busy={aiBusy === "title" || aiBusy === "all"}
                />
                <IconButton
                  icon="refresh"
                  label="Regenerate title"
                  title="Regenerate title"
                  disabled={aiBusy !== null || busy}
                  onClick={() => requestGenerate("title")}
                />
              </div>
            </div>
            <div className="field">
              <label htmlFor="pub-caption">Description</label>
              <div className="meta-row">
                <textarea
                  id="pub-caption"
                  rows={3}
                  value={captionVal}
                  onChange={(e) => editCaption(e.target.value)}
                  placeholder={initialCaption(titleVal || clipTitle, [])}
                  aria-busy={aiBusy === "description" || aiBusy === "all"}
                />
                <IconButton
                  icon="refresh"
                  label="Regenerate description"
                  title="Regenerate description"
                  disabled={aiBusy !== null || busy}
                  onClick={() => requestGenerate("description")}
                />
              </div>
            </div>
            <div className="field">
              <label htmlFor="pub-tags">Hashtags</label>
              <div className="meta-row">
                <input
                  id="pub-tags"
                  value={hashtagsVal}
                  onChange={(e) => editHashtags(e.target.value)}
                  placeholder="#shorts #indonesia"
                />
                <IconButton
                  icon="refresh"
                  label="Regenerate hashtags"
                  title="Regenerate hashtags"
                  disabled={aiBusy !== null || busy}
                  onClick={() => requestGenerate("hashtags")}
                />
              </div>
            </div>
            <div className="field">
              <span id="pub-when">Publishing</span>
              <div className="seg" role="group" aria-labelledby="pub-when">
                <button
                  type="button"
                  className="seg-btn"
                  aria-pressed={when === "now"}
                  onClick={() => setWhen("now")}
                >
                  Now
                </button>
                <button
                  type="button"
                  className="seg-btn"
                  aria-pressed={when === "schedule"}
                  onClick={() => setWhen("schedule")}
                >
                  Schedule
                </button>
              </div>
              {when === "schedule" && (
                <div className="field-row-3">
                  <div className="field">
                    <label htmlFor="pub-date">Date</label>
                    <input type="date" id="pub-date" value={schedDate} onChange={(e) => setSchedDate(e.target.value)} />
                  </div>
                  <div className="field">
                    <label htmlFor="pub-time">Time</label>
                    <input type="time" id="pub-time" value={schedTime} onChange={(e) => setSchedTime(e.target.value)} />
                  </div>
                  <div className="field">
                    <label htmlFor="pub-tz">Timezone</label>
                    <select id="pub-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)}>
                      {timeZoneList().map((tz) => (
                        <option key={tz} value={tz}>{tz}</option>
                      ))}
                    </select>
                  </div>
                </div>
              )}
            </div>
            {anyPrivacy && (
              <div className="field">
                <label htmlFor="pub-privacy">Privacy</label>
                <select id="pub-privacy" value={privacy} onChange={(e) => setPrivacy(e.target.value)}>
                  <option value="public">Public</option>
                  <option value="unlisted">Unlisted</option>
                  <option value="private">Private</option>
                </select>
                <small className="muted">Unlisted hanya YouTube; platform lain fallback ke private.</small>
              </div>
            )}
            </div>
            {issues.length > 0 && (
              <ul className="field-err" role="alert" aria-label="Validasi publish">
                {issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            )}
            <div className="dialog-actions">
              <button type="button" className="btn btn-text" onClick={() => requestClose()} disabled={busy || aiBusy !== null}>
                Batal
              </button>
              <button type="button" className="btn btn-filled" onClick={submit} disabled={!canSubmit} title={disableReason}>
                {busy ? "Memproses…" : allDraft ? "Save Draft" : "Publish"}
              </button>
            </div>
            <ConfirmDialog
              open={confirmScope !== null}
              title="Timpa hasil edit manual?"
              message="Some fields have been edited manually. Regenerate and replace them?"
              confirmLabel="Replace"
              busy={aiBusy !== null}
              onConfirm={() => {
                const scope = confirmScope;
                setConfirmScope(null);
                if (scope) void doGenerate(scope);
              }}
              onClose={() => setConfirmScope(null)}
            />
        </div>
      </div>
      )}
    </>
  );
}
