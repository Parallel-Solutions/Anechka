"""Extract batch metadata from Tomoru export filenames."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

_BATCH_RE = re.compile(
    r"^batch_([0-9a-fA-F]+)_(\d{8})T(\d{6})(?:\s*\(\d+\))?\.csv$",
    re.IGNORECASE,
)


@dataclass
class BatchFilenameMeta:
    batch_id: str | None
    exported_at: datetime | None
    warning: str | None = None


def parse_batch_filename(filename: str) -> BatchFilenameMeta:
    """Parse ``batch_{id}_{YYYYMMDD}T{HHMMSS}[(N)].csv`` variants."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    m = _BATCH_RE.match(base)
    if not m:
        return BatchFilenameMeta(
            batch_id=None,
            exported_at=None,
            warning="Имя файла не соответствует шаблону batch_*",
        )
    batch_id = m.group(1).lower()
    date_part = m.group(2)
    time_part = m.group(3)
    try:
        exported_at = datetime.strptime(f"{date_part}{time_part}", "%Y%m%d%H%M%S")
    except ValueError:
        return BatchFilenameMeta(
            batch_id=batch_id,
            exported_at=None,
            warning="Не удалось разобрать дату экспорта из имени файла",
        )
    return BatchFilenameMeta(batch_id=batch_id, exported_at=exported_at)
