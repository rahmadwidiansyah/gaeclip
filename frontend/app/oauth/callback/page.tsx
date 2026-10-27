"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";
import { apiBase } from "../../lib/api";
import { completeOauthCallback } from "../../lib/social";
import { useToast } from "../../lib/store";

/** OAuth landing page: Google redirects here with ?code=&state= (or error).
 * Forwards to the backend, which validates state, exchanges, and stores.
 * Same-tab navigation — nothing here can be popup-blocked. */
function CallbackInner() {
  const params = useSearchParams();
  const push = useToast();
  const [state, setState] = useState<"working" | "done" | "failed">("working");
  const [message, setMessage] = useState("Menyelesaikan login…");
  // StrictMode double-invokes effects in dev: never POST the same code twice.
  const submittedRef = useRef(false);

  useEffect(() => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    let alive = true;
    const code = params.get("code") ?? "";
    const st = params.get("state") ?? "";
    const err = params.get("error") ?? "";
    completeOauthCallback(apiBase(), { state: st, code, error: err })
      .then((res) => {
        if (!alive) return;
        if (res.status === "complete") {
          setState("done");
          setMessage("Login berhasil. Akun sudah terhubung.");
          push("Akun terhubung", "success");
        } else {
          setState("failed");
          setMessage(res.error || "Login gagal.");
        }
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setState("failed");
        const msg = e instanceof Error ? e.message : "Login gagal.";
        setMessage(msg);
        push(msg, "error");
      });
    return () => {
      alive = false;
    };
  }, [params, push]);

  return (
    <div className="card" role="status" aria-label="Status login">
      <h2>{state === "failed" ? "Login gagal" : state === "done" ? "Login berhasil" : "Menyelesaikan login…"}</h2>
      <p className="muted">{message}</p>
      {state !== "working" && (
        <p>
          <Link href="/settings">Kembali ke Settings</Link>
        </p>
      )}
    </div>
  );
}

export default function OAuthCallbackPage() {
  return (
    <Suspense fallback={<div className="card"><p className="muted">Menyelesaikan login…</p></div>}>
      <CallbackInner />
    </Suspense>
  );
}
