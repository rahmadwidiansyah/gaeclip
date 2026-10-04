"use client";

import { useEffect, useState } from "react";
import { resolveIcon, type IconName } from "../lib/icons";
import { STAGE_LABELS } from "../lib/types";

/* ── Icons (Lucide, single icon system) ──
   Size guideline: 14–16 compact metadata/action, 18–19 buttons,
   20 navigation, 24+ prominent. Default stroke, no filled/emoji mix.
   Name → component map lives in lib/icons.ts (unit-tested). */

export type { IconName };

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const Cmp = resolveIcon(name);
  return <Cmp size={size} strokeWidth={2} aria-hidden="true" />;
}

/* ── Status chip ── */

const STATUS_CLASS: Record<string, string> = {
  completed: "ok",
  partial: "warn",
  failed: "err",
  cancelled: "muted",
  expired: "muted",
  running: "info",
  queued: "info",
  accepted: "info",
  retrying: "warn",
};

export function StatusChip({ status, stage }: { status: string; stage?: string }) {
  const label = stage && STAGE_LABELS[stage] ? STAGE_LABELS[stage].label : status;
  return <span className={`chip chip-${STATUS_CLASS[status] ?? "muted"}`}>{label}</span>;
}

/* ── Progress bar ── */

export function ProgressBar({ value, label }: { value: number; label?: string }) {
  return (
    <div className="pbar" role="progressbar" aria-valuenow={value} aria-valuemin={0} aria-valuemax={100} aria-label={label ?? `Progress ${value}%`}>
      <div className="pbar-fill" style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}

/* ── Empty state ── */

export function EmptyState({ icon = "jobs", title, hint, action }: { icon?: string; title: string; hint?: string; action?: React.ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-icon" aria-hidden="true">
        <Icon name={icon} size={28} />
      </span>
      <p className="empty-title">{title}</p>
      {hint && <p className="empty-hint">{hint}</p>}
      {action}
    </div>
  );
}

/* ── Confirm dialog ── */

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "Ya, lanjutkan",
  danger = false,
  busy = false,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="dialog-scrim" onClick={onClose} role="presentation">
      <div className="dialog" role="alertdialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <h3 className="dialog-title">{title}</h3>
        <p className="dialog-msg">{message}</p>
        <div className="dialog-actions">
          <button type="button" className="btn btn-text" onClick={onClose} disabled={busy}>
            Batal
          </button>
          <button
            type="button"
            className={`btn ${danger ? "btn-danger" : "btn-filled"}`}
            onClick={onConfirm}
            disabled={busy}
            autoFocus
          >
            {busy ? "Memproses…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/* ── Collapsible section (secondary / debug info) ── */

export function Collapsible({
  title,
  subtitle,
  badge,
  defaultOpen = false,
  open: controlledOpen,
  onOpenChange,
  children,
}: {
  title: string;
  subtitle?: string;
  badge?: React.ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  children: React.ReactNode;
}) {
  const [uncontrolled, setUncontrolled] = useState(defaultOpen);
  const open = controlledOpen ?? uncontrolled;
  const toggle = () => {
    const next = !open;
    if (onOpenChange) onOpenChange(next);
    else setUncontrolled(next);
  };
  return (
    <section className="card coll" aria-label={title}>
      <button
        type="button"
        className="coll-head"
        aria-expanded={open}
        onClick={toggle}
      >
        <span className="coll-title">
          <strong>{title}</strong>
          {subtitle && <small className="muted">{subtitle}</small>}
        </span>
        <span className="coll-side">
          {badge}
          <span className={`coll-chev${open ? " open" : ""}`} aria-hidden="true">
            <Icon name="chevD" size={16} />
          </span>
        </span>
      </button>
      {open && <div className="coll-body">{children}</div>}
    </section>
  );
}

/* ── Pagination ── */

export function Pagination({
  page,
  totalPages,
  onPage,
}: {
  page: number;
  totalPages: number;
  onPage: (p: number) => void;
}) {
  const [jump, setJump] = useState("");
  if (totalPages <= 1) return null;
  return (
    <div className="pager">
      <button type="button" className="btn btn-icon" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Halaman sebelumnya" title="Sebelumnya">
        <Icon name="chevL" size={16} />
      </button>
      <span className="pager-info">
        Hal {page} / {totalPages}
      </span>
      <button
        type="button"
        className="btn btn-icon"
        disabled={page >= totalPages}
        onClick={() => onPage(page + 1)}
        aria-label="Halaman berikut"
        title="Berikutnya"
      >
        <Icon name="chevR" size={16} />
      </button>
      <form
        className="pager-jump"
        onSubmit={(e) => {
          e.preventDefault();
          const n = Number(jump);
          if (n >= 1 && n <= totalPages) onPage(n);
          setJump("");
        }}
      >
        <input value={jump} onChange={(e) => setJump(e.target.value)} placeholder="#" aria-label="Lompat ke halaman" inputMode="numeric" />
      </form>
    </div>
  );
}

/* ── Stat card ── */

export function Stat({ icon, label, value, tone }: { icon: string; label: string; value: string | number; tone?: string }) {
  return (
    <div className={`stat tone-${tone ?? "default"}`}>
      <span className="stat-icon" aria-hidden="true">
        <Icon name={icon} size={18} />
      </span>
      <span className="stat-body">
        <strong>{value}</strong>
        <small>{label}</small>
      </span>
    </div>
  );
}

/* ── Page header ── */

export function PageHeader({ eyebrow, title, desc, actions }: { eyebrow: string; title: string; desc?: string; actions?: React.ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1 className="page-title">{title}</h1>
        {desc && <p className="page-desc">{desc}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}
