"use client";

import { Icon, type IconName } from "../components/ui";

/**
 * Reusable provider account card (Settings → Social).
 * One visual language for all four providers: icon + name + text status
 * (never color alone), compact account line, contextual primary action,
 * guided needs-config and collapsible error states.
 */
export function SocialAccountCard({
  icon,
  title,
  statusTone,
  statusText,
  accountName,
  readiness,
  note,
  busy,
  busyLabel,
  primaryLabel,
  onPrimary,
  needsConfig,
  error,
  children,
}: {
  icon: IconName;
  title: string;
  statusTone: string;
  statusText: string;
  accountName?: string | null;
  readiness?: string | null;
  note?: string | null;
  busy?: boolean;
  busyLabel?: string;
  primaryLabel?: string | null;
  onPrimary?: (() => void) | null;
  needsConfig?: {
    message: string;
    actionLabel: string;
    onConfigure: () => void;
  } | null;
  error?: {
    message: string;
    detail?: string | null;
    onRetry: () => void;
  } | null;
  children?: React.ReactNode;
}) {
  return (
    <div className="prov-card">
      <div className="social-card-body">
        <div className="social-card-top">
          <Icon name={icon} size={22} />
          <strong>{title}</strong>
          <span className={`chip chip-${statusTone}`} style={{ marginLeft: "auto" }}>
            ● {statusText}
          </span>
        </div>
        {accountName && (
          <div className="muted" aria-label={`Akun terhubung ${accountName}`}>
            {accountName}
            {readiness && <> · {readiness}</>}
          </div>
        )}
        {note && (
          <small className="muted">{note}</small>
        )}
        {needsConfig && (
          <div className="social-guide" role="note">
            <p className="muted">{needsConfig.message}</p>
            <button type="button" className="btn btn-tonal btn-sm" onClick={needsConfig.onConfigure} disabled={busy}>
              {needsConfig.actionLabel}
            </button>
          </div>
        )}
        {error && (
          <div className="social-guide" role="alert">
            <p>
              <span className="chip chip-err">Connection failed</span>
            </p>
            <p className="muted">{error.message}</p>
            {error.detail && (
              <details>
                <summary>View details</summary>
                <p className="muted mono-sm">{error.detail}</p>
              </details>
            )}
            <button type="button" className="btn btn-tonal btn-sm" onClick={error.onRetry} disabled={busy}>
              Try again
            </button>
          </div>
        )}
        {children}
        {primaryLabel && onPrimary && !needsConfig && !error && (
          <div>
            <button
              type="button"
              className="btn btn-filled btn-sm"
              onClick={onPrimary}
              disabled={busy}
            >
              {busy && busyLabel ? busyLabel : primaryLabel}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
