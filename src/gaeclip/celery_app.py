from __future__ import annotations

import os

from celery import Celery

from .worker import process_job, refresh_video_metadata

app = Celery("gaeclip", broker=os.getenv("REDIS_URL", "redis://redis:6379/0"))
app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    # Ack AFTER completion: a killed/restarted worker requeues the task
    # instead of losing it. process_job is safe to re-run (overwrites).
    task_acks_late=True,
    # Don't hoard: with concurrency 1, prefetch only what the slot runs.
    worker_prefetch_multiplier=1,
)


@app.task(name="gaeclip.process_job")
def process(job_id: str):
    return process_job(job_id)


@app.task(name="gaeclip.refresh_metadata")
def refresh(job_id: str):
    return refresh_video_metadata(job_id)
