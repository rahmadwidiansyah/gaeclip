"use client";

import { useMemo, useState } from "react";
import { STAGE_ORDER, TERMINAL_STATES } from "../lib/types";
import type { Job } from "../lib/types";
import { Icon } from "./ui";

export type RoadState = "done" | "active" | "failed" | "pending" | "skipped";

export interface NgNode {
  key: string;
  title: string;
  sub?: string | null;
  state: RoadState;
  kind: "io" | "stage" | "ai" | "fallback" | "clip";
  reason?: string | null;
  anchor?: string;
}

export interface GraphData {
  input: NgNode;
  metadata: NgNode;
  subtitles: NgNode;
  ai: NgNode;
  fb: NgNode;
  clips: NgNode[];
  output: NgNode;
}

export function taskState(status: string): RoadState {
  if (status === "done") return "done";
  if (status === "failed") return "failed";
  if (status === "downloading" || status === "rendering" || status === "verifying") return "active";
  return "pending";
}

export function buildGraph(job: Job): GraphData {
  const jobStageIdx = STAGE_ORDER.indexOf(job.stage);
  const isTerminal = TERMINAL_STATES.includes(job.status);

  const stageState = (orderKeys: string[]): RoadState => {
    const idx = Math.max(...orderKeys.map((k) => STAGE_ORDER.indexOf(k)));
    if (job.status === "completed" || job.status === "partial") return "done";
    if (job.status === "failed" || job.status === "cancelled" || job.status === "expired") {
      if (jobStageIdx > idx) return "done";
      return "failed";
    }
    if (orderKeys.includes(job.stage)) return "active";
    if (jobStageIdx > idx) return "done";
    return "pending";
  };

  const liveSub = (state: RoadState) => (state === "active" ? job.stage_detail : null);
  const metadata = stageState(["metadata"]);
  const subtitles = stageState(["subtitles", "transcribing"]);
  const subtitlesSub =
    subtitles === "active"
      ? job.stage_detail
      : job.transcript_reused
        ? "Cache digunakan"
        : null;
  const cur = job.curation ?? null;

  let ai: NgNode;
  let fb: NgNode;
  if (!cur) {
    const s: RoadState = job.stage === "curating" && !isTerminal ? "active" : stageState(["curating"]);
    ai = { key: "ai", title: "AI Gemini", sub: s === "active" ? "Memilih hook…" : "Menunggu kurasi", state: s, kind: "ai" };
    fb = { key: "fb", title: "Fallback", sub: "Menunggu kurasi", state: s === "active" ? "pending" : s, kind: "fallback" };
  } else if (cur.source === "gemini") {
    const tok =
      cur.input_tokens != null || cur.output_tokens != null
        ? ` · ${cur.input_tokens ?? "?"}+${cur.output_tokens ?? "?"} tok`
        : "";
    ai = { key: "ai", title: "AI Gemini", sub: `${cur.model ?? "gemini"}${tok}`, state: "done", kind: "ai" };
    fb = {
      key: "fb",
      title: `Fallback · ${cur.heatmap_used ? "Heatmap" : "Deterministik"}`,
      sub: "Tidak dipakai",
      state: "skipped",
      kind: "fallback",
    };
  } else {
    ai = cur.attempted_provider
      ? { key: "ai", title: "AI Gemini", sub: "Gagal — klik untuk alasan", state: "failed", kind: "ai", reason: cur.fallback_reason }
      : { key: "ai", title: "AI Gemini", sub: "Tidak dicoba — klik alasan", state: "skipped", kind: "ai", reason: cur.fallback_reason };
    const fbTitle =
      cur.curation_method === "heuristic"
        ? "Fallback · Transcript Heuristic"
        : cur.curation_method === "deterministic"
          ? "Fallback · Deterministik"
          : `Fallback · ${cur.heatmap_used ? "YouTube Heatmap" : "Deterministik"}`;
    fb = {
      key: "fb",
      title: fbTitle,
      sub: "Jalur aktif — klik alasan",
      state: "done",
      kind: "fallback",
      reason: cur.fallback_reason,
    };
  }

  const tasks = job.clip_tasks ?? [];
  let clips: NgNode[];
  if (tasks.length > 0) {
    clips = tasks.map((task) => ({
      key: task.clip_id,
      title: task.title || task.clip_id,
      sub: task.stage_detail ?? null,
      state: taskState(task.status),
      kind: "clip" as const,
      anchor: task.clip_id,
    }));
  } else {
    const doneCount = job.clips?.length ?? 0;
    // Guard: total_clips pernah tercemar detik transkrip (mis. 4045) sebelum
    // backend fix — cap ke batas keras clip agar tidak render ribuan node.
    const rawTotal = Math.max(job.total_clips || 0, job.max_clips || 0, doneCount);
    const total = Math.min(rawTotal, 10);
    clips = Array.from({ length: total }, (_, i) => {
      let state: RoadState = "pending";
      if (i < doneCount) state = "done";
      else if (isTerminal) state = job.status === "completed" ? "done" : "failed";
      else if (i === doneCount && !isTerminal && (job.stage === "downloading" || job.stage === "rendering"))
        state = "active";
      return { key: `clip-${i}`, title: job.clips?.[i]?.title || `Clip ${i + 1}`, state, kind: "clip" as const };
    });
  }

  const doneClips = clips.filter((c) => c.state === "done").length;
  const output: NgNode = {
    key: "output",
    title: "Output",
    sub: clips.length === 0 ? "Menunggu" : `${doneClips}/${clips.length} MP4`,
    state:
      job.status === "completed" || job.status === "partial"
        ? "done"
        : clips.some((c) => c.state === "failed") && isTerminal
          ? "failed"
          : clips.some((c) => c.state === "active")
            ? "active"
            : "pending",
    kind: "io",
  };

  return {
    input: { key: "input", title: "Input · YouTube", sub: job.source_url, state: "done", kind: "io" },
    metadata: { key: "metadata", title: "Metadata", sub: liveSub(metadata), state: metadata, kind: "stage" },
    subtitles: { key: "subtitles", title: "Subtitle", sub: subtitlesSub, state: subtitles, kind: "stage" },
    ai,
    fb,
    clips,
    output,
  };
}

function NgLink({ state }: { state: RoadState }) {
  return (
    <svg className={`ng-link edge-${state}`} width="36" height="24" viewBox="0 0 36 24" aria-hidden="true">
      <path d="M2 12 C 13 12, 23 12, 32 12" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
      <circle cx="32" cy="12" r="3" fill="currentColor" stroke="none" />
    </svg>
  );
}

function NgCard({ node, onClick }: { node: NgNode; onClick?: () => void }) {
  const icon = node.state === "done" ? "check" : node.state === "failed" ? "close" : node.state === "active" ? "loader" : "circle";
  return (
    <div
      className={`ng-node ng-${node.state}${onClick ? " ng-clickable" : ""}`}
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => e.key === "Enter" && onClick() : undefined}
      title={onClick ? "Klik untuk detail" : undefined}
    >
      <span className="ng-dot" aria-hidden="true">
        <Icon name={icon} size={12} />
      </span>
      <span className="ng-meta">
        <strong>{node.title}</strong>
        {node.sub && <small>{node.sub}</small>}
      </span>
    </div>
  );
}

export function WorkflowGraph({
  job,
  onClipClick,
  onReason,
}: {
  job: Job;
  onClipClick?: (clipId: string) => void;
  onReason?: (kind: "ai" | "fb", text: string | null, title: string) => void;
}) {
  const g = useMemo(() => buildGraph(job), [job]);
  const intoBranch: RoadState =
    g.ai.state === "done" || g.fb.state === "done"
      ? "done"
      : g.ai.state === "active" || g.fb.state === "active"
        ? "active"
        : g.ai.state === "failed" || g.fb.state === "failed"
          ? "failed"
          : "pending";
  const outBranch: RoadState = g.clips.some((c) => c.state === "active")
    ? "active"
    : g.clips.length > 0 && g.clips.every((c) => c.state === "done")
      ? "done"
      : g.clips.some((c) => c.state === "done")
        ? "done"
        : g.clips.some((c) => c.state === "failed")
          ? "failed"
          : "pending";

  const [reason, setReason] = useState<{ kind: string; title: string; text: string | null } | null>(null);

  const showReason = (kind: "ai" | "fb") => {
    const node = kind === "ai" ? g.ai : g.fb;
    if (!node.reason) return;
    const panel = {
      kind,
      title: kind === "ai" ? "AI Gemini — kenapa?" : "Fallback — kenapa dipakai?",
      text: node.reason,
    };
    setReason(reason?.kind === kind ? null : panel);
    onReason?.(kind, node.reason, panel.title);
  };

  return (
    <div>
      <div className="ng-canvas" aria-label="Graph alur job">
        <div className="ng-col">
          <NgCard node={g.input} />
        </div>
        <NgLink state={g.metadata.state} />
        <div className="ng-col">
          <NgCard node={g.metadata} />
        </div>
        <NgLink state={g.subtitles.state} />
        <div className="ng-col">
          <NgCard node={g.subtitles} />
        </div>
        <NgLink state={intoBranch} />
        <div className="ng-col ng-branch">
          <span className="ng-branch-title">Kurasi</span>
          <NgCard node={g.ai} onClick={g.ai.reason ? () => showReason("ai") : undefined} />
          <NgCard node={g.fb} onClick={g.fb.reason ? () => showReason("fb") : undefined} />
        </div>
        <NgLink state={outBranch} />
        <div className="ng-col">
          {g.clips.length === 0 ? (
            <NgCard node={{ key: "noclip", title: "Clip", sub: "Menunggu kurasi", state: "pending", kind: "clip" }} />
          ) : (
            g.clips.map((clip) => (
              <NgCard key={clip.key} node={clip} onClick={clip.anchor && onClipClick ? () => onClipClick(clip.anchor as string) : undefined} />
            ))
          )}
        </div>
        <NgLink state={g.output.state} />
        <div className="ng-col">
          <NgCard node={g.output} />
        </div>
      </div>
      {reason && (
        <div className="ng-reason" role="status">
          <strong>{reason.title}</strong>
          <p>{reason.text}</p>
          <button type="button" className="btn btn-text btn-sm" onClick={() => setReason(null)}>
            Tutup
          </button>
        </div>
      )}
    </div>
  );
}
