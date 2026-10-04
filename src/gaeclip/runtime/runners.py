"""Job runners behind one interface.

- ``CeleryJobRunner``: the existing server path (Celery broker), used
  verbatim by the Docker edition. Untouched semantics.
- ``LocalJobRunner``: desktop path. Jobs run on background threads in the
  API process via ``worker.process_job``; cancellation flips the job
  record (which the pipeline already polls) plus a local event so
  queued jobs never start. Concurrency guard mirrors MAX_CONCURRENT_JOBS.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)


class CeleryJobRunner:
    kind = "celery"

    def submit(self, job_id: str, task_name: str = "process_job") -> str:
        if task_name == "refresh_metadata":
            from ..worker import refresh_task

            _, task = refresh_task()
        else:
            from ..worker import celery_task

            _, task = celery_task()
        async_result = task.delay(job_id)
        return getattr(async_result, "id", None) or job_id

    def revoke(self, task_id: str) -> None:
        from ..worker import celery_task

        app_celery, _ = celery_task()
        app_celery.control.revoke(task_id, terminate=True)

    def active_jobs(self) -> list[str]:
        return []

    def queued_jobs(self) -> list[str]:
        return []


class LocalJobRunner:
    """In-process FIFO runner for the desktop edition."""

    kind = "local"

    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls, client=None):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            if client is not None:
                cls._instance._client_override = client
            return cls._instance

    def __init__(self, client=None):
        # Singleton state is managed in __new__; kept for constructor parity.
        pass

    @classmethod
    def _reset_for_tests(cls) -> None:
        with cls._instance_lock:
            cls._instance = None

    def _init(self):
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}  # task_id -> job_id
        self._queued: list[tuple[str, str, str]] = []  # (task_id, kind, job_id)
        self._cancelled: set[str] = set()  # job_ids that must not start
        self._threads: dict[str, threading.Thread] = {}
        self._client_override = None

    def _repository(self):
        from ..jobs import JobRepository
        from .factory import make_client

        if self._client_override is not None:
            return JobRepository(self._client_override)
        return JobRepository(make_client())

    def _max_concurrent(self) -> int:
        try:
            return max(1, int(os.getenv("LOCAL_WORKER_CONCURRENCY", "1")))
        except ValueError:
            return 1

    def submit(self, job_id: str, task_name: str = "process_job") -> str:
        task_id = f"local-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._queued.append((task_id, task_name, job_id))
        self._pump()
        return task_id

    def _pump(self) -> None:
        with self._lock:
            running = sum(1 for thread in self._threads.values() if thread.is_alive())
            while self._queued and running < self._max_concurrent():
                task_id, task_name, job_id = self._queued.pop(0)
                if job_id in self._cancelled:
                    self._cancelled.discard(job_id)
                    continue
                thread = threading.Thread(
                    target=self._run, args=(task_id, task_name, job_id),
                    name=f"gaeclip-local-{job_id}", daemon=True)
                self._threads[task_id] = thread
                self._active[task_id] = job_id
                running += 1
                thread.start()

    def _run(self, task_id: str, task_name: str, job_id: str) -> None:
        try:
            if task_name == "refresh_metadata":
                from ..worker import refresh_video_metadata

                refresh_video_metadata(job_id)
            else:
                from ..worker import process_job

                process_job(job_id)
        except Exception:
            logger.exception("Local job %s failed", job_id)
        finally:
            with self._lock:
                self._active.pop(task_id, None)
                self._threads.pop(task_id, None)
            self._pump()

    def revoke(self, task_id: str) -> None:
        """Cancel by task id (falls back to job id). Running jobs observe
        the cancelled record via the existing repository polling; queued
        jobs are dropped before they start."""
        with self._lock:
            job_id = self._active.get(task_id, None)
            if job_id is None:
                for queued_task_id, _, queued_job_id in self._queued:
                    if queued_task_id == task_id or queued_job_id == task_id:
                        job_id = queued_job_id
                        break
                else:
                    job_id = task_id
            self._cancelled.add(job_id)
            self._queued = [item for item in self._queued if item[2] != job_id]
        try:
            repository = self._repository()
            record = repository.get(job_id)
            if record is not None and record.status not in (
                    "completed", "partial", "failed", "cancelled", "expired"):
                repository.transition(job_id, "cancelled", "cancelled",
                                      record.progress)
        except Exception:
            logger.warning("Local revoke bookkeeping failed for %s", job_id,
                           exc_info=True)

    def active_jobs(self) -> list[str]:
        with self._lock:
            self._prune()
            return list(self._active.values())

    def queued_jobs(self) -> list[str]:
        with self._lock:
            return [job_id for _, _, job_id in self._queued]

    def _prune(self) -> None:
        dead = [task_id for task_id, thread in self._threads.items()
                if not thread.is_alive()]
        for task_id in dead:
            self._active.pop(task_id, None)
            self._threads.pop(task_id, None)

    def wait_until_idle(self, timeout: float = 60) -> bool:
        """Test/diagnostics helper: block until no work remains."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                self._prune()
                if not self._active and not self._queued:
                    return True
            time.sleep(0.05)
        return False
