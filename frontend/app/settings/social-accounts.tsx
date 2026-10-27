"use client";

import { useCallback, useEffect, useState } from "react";
import { Collapsible, ConfirmDialog, IconButton } from "../components/ui";
import {
  connectSocialAccount,
  disconnectSocialAccount,
  listSocialAccounts,
  listSocialProviders,
  reconnectSocialAccount,
  statusLabel,
  statusTone,
  updateSocialAccountPage,
  type SocialAccount,
  type SocialCapabilities,
} from "../lib/social";
import { SocialAccountCard } from "./social-account-card";

export const PROVIDER_LABEL: Record<string, string> = {
  youtube: "YouTube",
  tiktok: "TikTok",
  instagram: "Instagram",
  facebook: "Facebook",
};

export const PROVIDER_ICON: Record<string, "youtube" | "tiktok" | "instagram" | "facebook"> = {
  youtube: "youtube",
  tiktok: "tiktok",
  instagram: "instagram",
  facebook: "facebook",
};

export const PROVIDER_PORTAL: Record<string, { label: string; href: string }> = {
  youtube: { label: "Buat Client ID Google", href: "https://console.cloud.google.com/apis/credentials" },
  tiktok: { label: "Daftarkan app TikTok", href: "https://developers.tiktok.com/" },
  instagram: { label: "Siapkan Instagram Login", href: "https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login" },
  facebook: { label: "Buat Meta App", href: "https://developers.facebook.com/apps/" },
};

const PROVIDER_HINT: Record<string, string> = {
  youtube: "Upload Shorts + judul, deskripsi, privasi, jadwal.",
  tiktok: "Draft only sampai app TikTok lolos audit.",
  instagram: "Login Instagram langsung (Business Login). Butuh akun Bisnis/Kreator + Instagram App sendiri.",
  facebook: "Login Facebook (butuh Halaman/Page). Koneksi terpisah dari Instagram.",
};

type CardError = { message: string; detail?: string | null };

export default function SocialAccountsSection({
  apiBase,
  push,
  onNeedConfig,
}: {
  apiBase: string;
  push: (message: string, kind?: "info" | "success" | "error") => void;
  onNeedConfig?: (provider: string) => void;
}) {
  const [caps, setCaps] = useState<Record<string, SocialCapabilities> | null>(null);
  const [accounts, setAccounts] = useState<SocialAccount[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmDrop, setConfirmDrop] = useState<SocialAccount | null>(null);
  const [needsConfig, setNeedsConfig] = useState<Record<string, boolean>>({});
  const [cardError, setCardError] = useState<Record<string, CardError | null>>({});

  const refresh = useCallback(async () => {
    try {
      const [c, a] = await Promise.all([listSocialProviders(apiBase), listSocialAccounts(apiBase)]);
      setCaps(c.providers);
      setAccounts(a.accounts);
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal memuat akun social", "error");
    }
  }, [apiBase, push]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Same-tab OAuth: POST returns the login URL synchronously, then the
  // CURRENT tab navigates there (never popup-blocked). Google lands back
  // on /oauth/callback in this same tab.
  const goConnect = async (provider: string, starter: Promise<{ authorization_url?: string }>, busyKey: string) => {
    setBusy(busyKey);
    setCardError((m) => ({ ...m, [provider]: null }));
    try {
      const s = await starter;
      if (!s.authorization_url) throw new Error("Backend tidak memberi URL login.");
      window.location.href = s.authorization_url;
    } catch (e) {
      setBusy(null);
      const code = (e as Error & { code?: string })?.code;
      if (code === "oauth_not_configured") {
        setNeedsConfig((m) => ({ ...m, [provider]: true }));
      } else {
        setCardError((m) => ({
          ...m,
          [provider]: { message: e instanceof Error ? e.message : "Gagal memulai login" },
        }));
      }
      push(e instanceof Error ? e.message : "Gagal memulai login", "error");
    }
  };

  const startConnect = (provider: string) => {
    const redirectUri = `${window.location.origin}/oauth/callback`;
    void goConnect(provider, connectSocialAccount(apiBase, provider, redirectUri), `connect-${provider}`);
  };

  const startReconnect = (account: SocialAccount) => {
    const redirectUri = `${window.location.origin}/oauth/callback`;
    void goConnect(account.provider, reconnectSocialAccount(apiBase, account.id, redirectUri), `reconnect-${account.id}`);
  };

  const doDisconnect = async () => {
    if (!confirmDrop) return;
    setBusy(`drop-${confirmDrop.id}`);
    try {
      await disconnectSocialAccount(apiBase, confirmDrop.id);
      push("Akun diputus", "success");
      refresh();
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal disconnect", "error");
    } finally {
      setBusy(null);
      setConfirmDrop(null);
    }
  };

  const changePage = async (account: SocialAccount, facebookPageId: string) => {
    if (!facebookPageId || facebookPageId === String((account.meta as Record<string, unknown> | undefined)?.["facebook_page_id"] ?? account.external_account_id)) return;
    setBusy(`page-${account.id}`);
    try {
      await updateSocialAccountPage(apiBase, account.id, facebookPageId);
      push("Page diganti", "success");
      refresh();
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal ganti Page", "error");
    } finally {
      setBusy(null);
    }
  };

  const providers = caps ? Object.keys(caps) : ["youtube", "tiktok", "instagram", "facebook"];
  const loading = caps === null || accounts === null;
  const connectedCount = (accounts ?? []).filter((a) => a.status === "connected").length;

  return (
    <>
      <div className="social-head" aria-live="polite">
        {loading ? (
          <p className="muted">Memuat provider & akun…</p>
        ) : connectedCount > 0 ? (
          <p>
            <span className="chip chip-ok">● {connectedCount} account{connectedCount > 1 ? "s" : ""} connected</span>
          </p>
        ) : (
          <p className="muted">○ No accounts connected — hubungkan satu akun untuk mulai publish.</p>
        )}
      </div>
      {loading ? null : (
        <div className="grid-2" role="list" aria-label="Provider social">
          {providers.map((provider) => {
            const cap = caps?.[provider];
            const mine = (accounts ?? []).filter((a) => a.provider === provider);
            const first = mine.find((a) => a.status === "connected") ?? mine[0] ?? null;
            const connecting = busy === `connect-${provider}`;
            const draftOnly = provider === "tiktok" && !!cap && !cap.direct_publish;
            // Legacy Instagram rows (connected via Facebook Login before the
            // Instagram Login migration) must reconnect — never reused.
            const legacyIG = provider === "instagram" && !!first &&
              (first.meta as Record<string, unknown> | undefined)?.["login_flow"] !== "instagram_login";
            const status = connecting
              ? { tone: "info", text: "Connecting…" }
              : cardError[provider]
                ? { tone: "err", text: "Connection failed" }
                : first
                  ? { tone: statusTone(first.status), text: statusLabel(first.status) }
                  : { tone: "muted", text: "Not connected" };
            return (
              <div key={provider} role="listitem">
                <SocialAccountCard
                  icon={PROVIDER_ICON[provider] ?? "link"}
                  title={PROVIDER_LABEL[provider] ?? provider}
                  statusTone={status.tone}
                  statusText={status.text}
                  accountName={first?.display_name || first?.external_account_id || null}
                  readiness={first && first.status === "connected" && !legacyIG ? "Ready to publish" : null}
                  note={legacyIG ? "Koneksi lama via Facebook — klik Reconnect untuk login via Instagram." : (PROVIDER_HINT[provider] ?? null)}
                  busy={busy !== null}
                  busyLabel={connecting ? "Membuka login…" : undefined}
                  primaryLabel={first ? undefined : `Connect ${PROVIDER_LABEL[provider] ?? provider}`}
                  onPrimary={first ? undefined : () => startConnect(provider)}
                  needsConfig={
                    needsConfig[provider]
                      ? {
                          message: `${PROVIDER_LABEL[provider] ?? provider} belum dikonfigurasi. Tambahkan Client ID dulu untuk menghubungkan akun.`,
                          actionLabel: `Configure ${PROVIDER_LABEL[provider] ?? provider}`,
                          onConfigure: () => {
                            setNeedsConfig((m) => ({ ...m, [provider]: false }));
                            onNeedConfig?.(provider);
                          },
                        }
                      : null
                  }
                  error={
                    cardError[provider]
                      ? {
                          message: cardError[provider]?.message ?? "Login gagal.",
                          onRetry: () => startConnect(provider),
                        }
                      : null
                  }
                >
                  {first && (
                    <div className="row-line">
                      <span className="row-actions" style={{ marginLeft: 0 }}>
                        <IconButton
                          icon="refresh"
                          label={first.status === "connected" ? "Reconnect akun" : "Hubungkan ulang akun"}
                          title="Reconnect"
                          disabled={busy !== null}
                          onClick={() => startReconnect(first)}
                        />
                        <IconButton
                          icon="trash"
                          label="Putus akun"
                          title="Disconnect"
                          variant="danger"
                          disabled={busy !== null}
                          onClick={() => setConfirmDrop(first)}
                        />
                      </span>
                      {draftOnly && (
                        <small className="muted">Draft only — direct post perlu audit TikTok.</small>
                      )}
                    </div>
                  )}
                  {first && provider === "facebook" && Array.isArray((first.meta as Record<string, unknown> | undefined)?.["page_choices"] as unknown[]) && ((first.meta as Record<string, unknown>)["page_choices"] as { id: string; name: string }[]).length > 1 && (
                    <div className="row-line">
                      <label className="muted" htmlFor={`page-${first.id}`}>Page:</label>
                      <select
                        id={`page-${first.id}`}
                        value={String((first.meta as Record<string, unknown>)?.["facebook_page_id"] ?? first.external_account_id)}
                        disabled={busy !== null}
                        onChange={(e) => void changePage(first, e.target.value)}
                      >
                        {((first.meta as Record<string, unknown>)["page_choices"] as { id: string; name: string }[]).map((p) => (
                          <option key={String(p.id)} value={String(p.id)}>
                            {String(p.name || p.id)}
                          </option>
                        ))}
                      </select>
                    </div>
                  )}
                </SocialAccountCard>
              </div>
            );
          })}
        </div>
      )}
      <ConfirmDialog
        open={confirmDrop !== null}
        title="Putus akun?"
        message={`Akun ${confirmDrop?.display_name || ""} dan token tersimpannya akan dihapus. Publish terjadwal milik akun ini ikut batal.`}
        confirmLabel="Putus"
        danger
        busy={busy !== null}
        onConfirm={doDisconnect}
        onClose={() => setConfirmDrop(null)}
      />
    </>
  );
}
