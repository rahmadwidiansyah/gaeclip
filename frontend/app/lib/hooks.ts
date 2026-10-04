"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getJob, listJobs } from "./api";
import { useSettings } from "./store";
import type { Job } from "./types";

export function useJobs(limit = 100) {
  const { apiBase, pollMs } = useSettings();
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await listJobs(apiBase, limit);
      setJobs(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Gagal memuat job");
    } finally {
      setLoading(false);
    }
  }, [apiBase, limit]);

  useEffect(() => {
    setLoading(true);
    refresh();
    if (pollMs <= 0) return;
    const timer = window.setInterval(refresh, pollMs);
    return () => window.clearInterval(timer);
  }, [refresh, pollMs]);

  return { jobs, loading, error, refresh };
}

export function useJob(jobId: string) {
  const { apiBase, pollMs } = useSettings();
  const [job, setJob] = useState<Job | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const activeRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const data = await getJob(apiBase, jobId);
      if (activeRef.current) {
        setJob(data);
        setError(null);
      }
    } catch (e) {
      if (activeRef.current) setError(e instanceof Error ? e.message : "Gagal memuat job");
    } finally {
      if (activeRef.current) setLoading(false);
    }
  }, [apiBase, jobId]);

  useEffect(() => {
    activeRef.current = true;
    setLoading(true);
    refresh();
    if (pollMs <= 0) return;
    const timer = window.setInterval(async () => {
      try {
        const data = await getJob(apiBase, jobId);
        if (!activeRef.current) return;
        setJob(data);
        const terminal = ["completed", "partial", "failed", "cancelled", "expired"];
        if (terminal.includes(data.status)) window.clearInterval(timer);
      } catch {
        /* keep polling */
      }
    }, pollMs);
    return () => {
      activeRef.current = false;
      window.clearInterval(timer);
    };
  }, [refresh, pollMs, apiBase, jobId]);

  return { job, loading, error, refresh };
}
