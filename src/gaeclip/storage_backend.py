"""Storage backend protocol for future object-storage support.

Gaeclip currently stores all artifacts on the local filesystem under
``STORAGE_ROOT``. This protocol defines the interface that a future
S3-compatible backend would implement. The plan is to replace the
direct ``Path`` usage in ``gaeclip.storage`` and ``gaeclip.api`` with
an injected backend instance, but for now the protocol serves as a
contract for contributors who want to add cloud storage support.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO


class StorageBackend(ABC):
    """Minimal object-storage interface."""

    @abstractmethod
    def upload(self, key: str, data: BinaryIO, size: int) -> None:
        """Upload a byte stream to *key*."""

    @abstractmethod
    def download(self, key: str) -> tuple[BinaryIO, int]:
        """Return (stream, size) for *key*."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete *key* if it exists."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Check whether *key* exists."""

    @abstractmethod
    def list_prefix(self, prefix: str) -> list[str]:
        """Return keys under *prefix*."""


class LocalStorageBackend(StorageBackend):
    """Drop-in replacement that writes to ``STORAGE_ROOT``."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    def _path(self, key: str) -> Path:
        return (self.root / key).resolve()

    def upload(self, key: str, data: BinaryIO, size: int) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            while True:
                chunk = data.read(8192)
                if not chunk:
                    break
                f.write(chunk)

    def download(self, key: str) -> tuple[BinaryIO, int]:
        path = self._path(key)
        size = path.stat().st_size
        f = path.open("rb")
        return f, size

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def list_prefix(self, prefix: str) -> list[str]:
        root = self.root
        prefix_path = root / prefix
        if not prefix_path.is_dir():
            return []
        return [str(p.relative_to(root)) for p in prefix_path.rglob("*") if p.is_file()]
