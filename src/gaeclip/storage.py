from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

from .errors import ValidationError


def new_job_id() -> str:
    return f"job_{secrets.token_urlsafe(12)}"


class JobStorage:
    def __init__(self, root: Path, job_id: str):
        if "/" in job_id or "\\" in job_id or job_id.startswith("."):
            raise ValidationError("Invalid job ID")
        self.root = root.resolve()
        self.job_root = (self.root / "jobs" / job_id).resolve()
        if not self.job_root.is_relative_to(self.root):
            raise ValidationError("Job path escapes storage root")

    def create(self) -> Path:
        self.job_root.mkdir(parents=True, exist_ok=False)
        os.chmod(self.job_root, 0o700)
        return self.job_root

    def path(self, filename: str) -> Path:
        candidate = (self.job_root / filename).resolve()
        if not candidate.is_relative_to(self.job_root):
            raise ValidationError("Artifact path escapes job root")
        return candidate

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
