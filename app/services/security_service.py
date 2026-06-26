"""Маскирование данных и безопасность файлов."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def mask_webhook(url: str) -> str:
    if not url:
        return ""
    url = url.rstrip("/")
    parts = url.split("/")
    if len(parts) >= 2:
        parts[-1] = "***"
        parts[-2] = "***"
    return "/".join(parts)


def mask_secret(value: str, visible_tail: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= visible_tail:
        return "***"
    return f"{'*' * 8}{value[-visible_tail:]}"


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return "***"
    if phone.strip().startswith("+"):
        return f"+{digits[0]}******{digits[-4:]}"
    return f"{digits[0]}******{digits[-4:]}"


def safe_filename(mode: str, label: str, ext: str = "xlsx") -> str:
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = _slugify(label) or "export"
    name = f"{ts}_{mode}_{slug}.{ext}"
    return UNSAFE_FILENAME.sub("_", name)[:200]


def _slugify(value: str, max_len: int = 60) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^\w\-]+", "_", ascii_text, flags=re.UNICODE)
    slug = re.sub(r"_+", "_", slug).strip("_")
    if slug:
        return slug[:max_len]
    slug = re.sub(r"\s+", "_", value.strip())
    slug = re.sub(r"[^\w\-]+", "_", slug, flags=re.UNICODE)
    return slug.strip("_")[:max_len] or "item"


def unique_filepath(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    counter = 1
    while True:
        candidate = directory / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def validate_download_path(export_dir: Path, result_file: str) -> Path:
    if not result_file:
        raise ValueError("Файл не указан")
    file_path = Path(result_file)
    if not file_path.is_absolute():
        file_path = (export_dir / file_path).resolve()
    else:
        file_path = file_path.resolve()
    export_resolved = export_dir.resolve()
    if export_resolved not in file_path.parents and file_path.parent != export_resolved:
        raise ValueError("Недопустимый путь к файлу")
    if not file_path.is_file():
        raise ValueError("Файл не найден")
    return file_path


def format_local_dt(dt: datetime | None, tz_name: str = "Europe/Moscow") -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz_name)).strftime("%d.%m.%Y %H:%M:%S")


def sanitize_excel_value(value: str | int | float | None) -> str | int | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@"):
        return "'" + text
    return text
