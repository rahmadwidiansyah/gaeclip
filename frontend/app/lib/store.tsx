"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Icon } from "../components/ui";
import { defaultApiBase } from "./api";

/* ── Settings ── */

export type Theme = "system" | "light" | "dark";

interface Settings {
  apiBase: string;
  pollMs: number;
  theme: Theme;
  setApiBase: (v: string) => void;
  setPollMs: (v: number) => void;
  setTheme: (v: Theme) => void;
  reset: () => void;
}

const SettingsContext = createContext<Settings | null>(null);

const DEFAULT_POLL = 2000;

function readLS(key: string, fallback: string): string {
  if (typeof window === "undefined") return fallback;
  return localStorage.getItem(key) ?? fallback;
}

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [apiBase, setApiBaseState] = useState(() => readLS("gaeclip_api_base", defaultApiBase()));
  const [pollMs, setPollMsState] = useState(() => Number(readLS("gaeclip_poll_ms", String(DEFAULT_POLL))) || DEFAULT_POLL);
  const [theme, setThemeState] = useState<Theme>(() => (readLS("gaeclip_theme", "system") as Theme) || "system");

  useEffect(() => {
    const root = document.documentElement;
    const apply = () => {
      const dark =
        theme === "dark" ||
        (theme === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
      root.dataset.theme = dark ? "dark" : "light";
    };
    apply();
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => theme === "system" && apply();
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, [theme]);

  const setApiBase = useCallback((v: string) => {
    setApiBaseState(v);
    localStorage.setItem("gaeclip_api_base", v);
  }, []);
  const setPollMs = useCallback((v: number) => {
    setPollMsState(v);
    localStorage.setItem("gaeclip_poll_ms", String(v));
  }, []);
  const setTheme = useCallback((v: Theme) => {
    setThemeState(v);
    localStorage.setItem("gaeclip_theme", v);
  }, []);
  const reset = useCallback(() => {
    localStorage.removeItem("gaeclip_api_base");
    localStorage.removeItem("gaeclip_poll_ms");
    localStorage.removeItem("gaeclip_theme");
    setApiBaseState(defaultApiBase());
    setPollMsState(DEFAULT_POLL);
    setThemeState("system");
  }, []);

  const value = useMemo(
    () => ({ apiBase, pollMs, theme, setApiBase, setPollMs, setTheme, reset }),
    [apiBase, pollMs, theme, setApiBase, setPollMs, setTheme, reset]
  );
  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): Settings {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings must be used inside SettingsProvider");
  return ctx;
}

/* ── Toasts ── */

export interface Toast {
  id: number;
  message: string;
  kind: "success" | "error" | "info";
}

const ToastContext = createContext<{ toasts: Toast[]; push: (message: string, kind?: Toast["kind"]) => void } | null>(null);

let toastId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((message: string, kind: Toast["kind"] = "info") => {
    const id = ++toastId;
    setToasts((t) => [...t.slice(-2), { id, message, kind }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);
  const value = useMemo(() => ({ toasts, push }), [toasts, push]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-host" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            <Icon name={t.kind === "success" ? "check" : t.kind === "error" ? "alert" : "info"} size={15} />
            <span>{t.message}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside ToastProvider");
  return ctx.push;
}
