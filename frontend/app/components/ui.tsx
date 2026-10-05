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

/* ── IconButton (design-system icon-only action) ──
   Variants:
   - secondary: bordered surface button (default for list actions:
     move up/down, edit, refresh, expand, add). Always visible on
     both light + dark theme — never white-on-white.
   - ghost: transparent until hover (toolbar/secondary affordances).
   - danger: destructive actions (delete/remove). Visually distinct
     from move/edit via error tones, not color alone (see CSS:
     border + icon tint).
   - primary: filled accent for the single primary row action.
   Every instance REQUIRES `label` (aria-label) and renders `title`
   as tooltip so icon-only actions are never a guessing game. */

export type IconButtonVariant = "secondary" | "ghost" | "danger" | "primary";
export type IconButtonSize = "sm" | "md";

export function IconButton({
  icon,
  label,
  title,
  variant = "secondary",
  size = "sm",
  iconSize,
  ...rest
}: {
  icon: IconName;
  /** Accessible name — required. Tooltip falls back to this. */
  label: string;
  title?: string;
  variant?: IconButtonVariant;
  size?: IconButtonSize;
  iconSize?: number;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "title">) {
  return (
    <button
      type="button"
      {...rest}
      className={`iconbtn iconbtn-${variant} iconbtn-${size}${rest.className ? ` ${rest.className}` : ""}${rest.disabled ? " is-disabled" : ""}`}
      aria-label={rest["aria-label"] ?? label}
      title={title ?? label}
    >
      <Icon name={icon} size={iconSize ?? (size === "sm" ? 14 : 16)} />
    </button>
  );
}

/* ── SearchInput (icon + clear-X contract) ──
   - Leading search glyph, trailing clear button shown ONLY when
     `value` is non-empty.
   - Clear resets the query ("", not a visual-only wipe) and calls
     `onClear` so callers can also reset pagination/filter state. */

export function SearchInput({
  value,
  onChange,
  onClear,
  placeholder = "Cari…",
  ariaLabel = "Cari",
  clearLabel = "Bersihkan pencarian",
  id,
  disabled = false,
  onFocus,
  onBlur,
}: {
  value: string;
  onChange: (next: string) => void;
  onClear?: () => void;
  placeholder?: string;
  ariaLabel?: string;
  clearLabel?: string;
  id?: string;
  disabled?: boolean;
  onFocus?: () => void;
  onBlur?: () => void;
}) {
  return (
    <div className="search-input">
      <span className="search-input-icon" aria-hidden="true">
        <Icon name="search" size={16} />
      </span>
      <input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={onFocus}
        onBlur={onBlur}
        placeholder={placeholder}
        aria-label={ariaLabel}
        disabled={disabled}
        autoComplete="off"
        spellCheck={false}
      />
      {value !== "" && (
        <button
          type="button"
          className="search-input-clear"
          onClick={() => {
            onChange("");
            onClear?.();
          }}
          aria-label={clearLabel}
          title={clearLabel}
          disabled={disabled}
        >
          <Icon name="close" size={14} />
        </button>
      )}
    </div>
  );
}

/* ── Pagination ──
   Numbered, keyboard-friendly pager shared by every long list.
   Backward compatible: <Pagination page totalPages onPage /> keeps
   working (jump box + Hal x/y). New callers pass `total` (+`pageSize`
   +`onPageSizeChange`) to get numbered buttons and a page-size
   selector; the indicator then reads from the real item count. */

export const PAGINATION_SIZE_OPTIONS = [10, 20, 50] as const;

function pageWindow(current: number, totalPages: number, maxButtons = 5): number[] {
  const total = Math.max(1, totalPages);
  const cur = Math.min(total, Math.max(1, current));
  const size = Math.min(maxButtons, total);
  let start = cur - Math.floor(size / 2);
  start = Math.max(1, Math.min(start, total - size + 1));
  return Array.from({ length: size }, (_, i) => start + i);
}

export function Pagination({
  page,
  totalPages,
  onPage,
  total,
  pageSize,
  onPageSizeChange,
  maxButtons = 5,
}: {
  page: number;
  totalPages: number;
  onPage: (p: number) => void;
  /** Real item count — enables numbered buttons + range label. */
  total?: number;
  pageSize?: number;
  onPageSizeChange?: (n: number) => void;
  maxButtons?: number;
}) {
  const [jump, setJump] = useState("");
  if (totalPages <= 1) return null;
  const safePage = Math.min(totalPages, Math.max(1, page));
  const numbers = pageWindow(safePage, totalPages, maxButtons);
  return (
    <div className="pager" role="navigation" aria-label="Pagination">
      <button type="button" className="iconbtn iconbtn-secondary iconbtn-sm" disabled={safePage <= 1} onClick={() => onPage(safePage - 1)} aria-label="Halaman sebelumnya" title="Halaman sebelumnya">
        <Icon name="chevL" size={16} />
      </button>
      <span className="pager-numbers" role="list">
        {numbers.map((n) => (
          <button
            key={n}
            type="button"
            role="listitem"
            className={`pager-num${n === safePage ? " is-current" : ""}`}
            aria-label={`Halaman ${n}`}
            aria-current={n === safePage ? "page" : undefined}
            onClick={() => onPage(n)}
            disabled={n === safePage}
          >
            {n}
          </button>
        ))}
      </span>
      <button
        type="button"
        className="iconbtn iconbtn-secondary iconbtn-sm"
        disabled={safePage >= totalPages}
        onClick={() => onPage(safePage + 1)}
        aria-label="Halaman berikut"
        title="Halaman berikut"
      >
        <Icon name="chevR" size={16} />
      </button>
      <span className="pager-info">
        {typeof total === "number" ? `${total} item · ` : ""}Hal {safePage} / {totalPages}
      </span>
      {pageSize != null && onPageSizeChange && (
        <label className="pager-size">
          <span className="sr-only">Item per halaman</span>
          <select value={pageSize} onChange={(e) => onPageSizeChange(Number(e.target.value))} aria-label="Item per halaman" title="Item per halaman">
            {PAGINATION_SIZE_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}/hal
              </option>
            ))}
          </select>
        </label>
      )}
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
