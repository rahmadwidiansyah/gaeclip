"use client";

import { useState } from "react";
import { patchAppSettings, resetAppSetting } from "../lib/api";
import type { AppSetting } from "../lib/types";

/** Reusable masked secret field (keep/replace/remove), mirroring the
 * Gemini API key UX: value never displayed, per-secret immediate save.
 * Use for any secret-type setting (Gemini key now, social OAuth secrets). */
export function SecretField({
  apiBase,
  entry,
  push,
  onChanged,
}: {
  apiBase: string;
  entry: AppSetting;
  push: (message: string, kind?: "info" | "success" | "error") => void;
  onChanged: (entries: AppSetting[]) => void;
}) {
  const [mode, setMode] = useState<"keep" | "replace" | "remove">("keep");
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (mode === "replace" && draft.trim() === "") {
      push("Secret pengganti masih kosong", "error");
      return;
    }
    setBusy(true);
    try {
      const fresh =
        mode === "remove"
          ? await resetAppSetting(apiBase, entry.key)
          : await patchAppSettings(apiBase, { [entry.key]: draft.trim() });
      onChanged(fresh);
      setMode("keep");
      setDraft("");
      push(mode === "remove" ? "Secret dihapus" : "Secret tersimpan", "success");
    } catch (e) {
      push(e instanceof Error ? e.message : "Gagal menyimpan secret", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="secret-box">
      <div className="secret-head">
        <div>
          <strong>{entry.label}</strong>{" "}
          <span className={`chip chip-${entry.configured ? "ok" : "muted"}`}>
            {entry.configured ? "Terkonfigurasi" : "Belum dikonfigurasi"}
          </span>
        </div>
        <div className="secret-actions">
          {entry.configured && mode === "keep" && (
            <>
              <button type="button" className="btn btn-tonal btn-sm" onClick={() => setMode("replace")} disabled={busy}>
                Ganti
              </button>
              <button type="button" className="btn btn-text btn-sm" onClick={() => setMode("remove")} disabled={busy}>
                Hapus
              </button>
            </>
          )}
          {!entry.configured && mode === "keep" && (
            <button type="button" className="btn btn-tonal btn-sm" onClick={() => setMode("replace")} disabled={busy}>
              Isi secret
            </button>
          )}
          {mode !== "keep" && (
            <button
              type="button"
              className="btn btn-text btn-sm"
              onClick={() => {
                setMode("keep");
                setDraft("");
              }}
              disabled={busy}
            >
              Batal
            </button>
          )}
        </div>
      </div>
      <div className="secret-value mono-sm" aria-label={`Status ${entry.label}`}>
        {mode === "remove"
          ? "Akan dihapus saat disimpan."
          : mode === "replace"
            ? "••••••••••••••••"
            : entry.configured
              ? "••••••••••••••••"
              : "Belum ada secret."}
      </div>
      {mode === "replace" && (
        <>
          <input
            type="password"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={`Tempel ${entry.label} baru…`}
            aria-label={entry.label}
            disabled={busy}
          />
          <div className="secret-actions" style={{ marginTop: 8 }}>
            <button type="button" className="btn btn-filled btn-sm" onClick={save} disabled={busy || draft.trim() === ""}>
              {busy ? "Menyimpan…" : "Simpan secret"}
            </button>
          </div>
        </>
      )}
      {mode === "remove" && (
        <div className="secret-actions" style={{ marginTop: 8 }}>
          <button type="button" className="btn btn-danger btn-sm" onClick={save} disabled={busy}>
            {busy ? "Menghapus…" : "Ya, hapus"}
          </button>
        </div>
      )}
      <p className="muted">
        <small>{entry.description}</small>
      </p>
    </div>
  );
}
