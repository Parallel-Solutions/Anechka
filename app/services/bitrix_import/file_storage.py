"""File storage abstraction for imported Bitrix files."""

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import Settings, get_file_storage_dir

logger = logging.getLogger(__name__)


class FileStorage(ABC):
    @abstractmethod
    def save(self, portal_id: str, file_id: str, file_name: str, content: bytes) -> tuple[str, str]:
        """Returns (storage_path, checksum)."""

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        ...


class LocalFileStorage(FileStorage):
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def _portal_dir(self, portal_id: str) -> Path:
        safe = portal_id.replace(":", "_").replace("/", "_")
        d = self.base_dir / safe
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, portal_id: str, file_id: str, file_name: str, content: bytes) -> tuple[str, str]:
        checksum = hashlib.sha256(content).hexdigest()
        portal_dir = self._portal_dir(portal_id)
        safe_name = file_name.replace("\\", "_").replace("/", "_")[:200] or "file"
        target = portal_dir / f"{file_id}_{safe_name}"
        if not target.exists():
            target.write_bytes(content)
        rel = str(target.relative_to(self.base_dir))
        return rel, checksum

    def exists(self, storage_path: str) -> bool:
        return (self.base_dir / storage_path).is_file()


def get_file_storage(settings: Settings | None = None) -> FileStorage:
    return LocalFileStorage(get_file_storage_dir(settings))
