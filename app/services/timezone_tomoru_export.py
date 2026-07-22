"""Create a ZIP with separate Tomoru phone lists for each timezone."""

from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from app.services.external_phone_import import build_tomoru_csv
from app.services.security_service import safe_filename, unique_filepath


def write_timezone_zip(
    rows: Iterable[Any],
    export_dir: Path,
    base_label: str,
    *,
    local_call_time: str = "10:00",
) -> Path:
    groups: dict[str, list[str]] = defaultdict(list)
    seen_by_timezone: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        timezone_name = str(getattr(row, "timezone", None) or "Europe/Moscow")
        phone = str(getattr(row, "phone", "") or "")
        if phone and phone not in seen_by_timezone[timezone_name]:
            seen_by_timezone[timezone_name].add(phone)
            groups[timezone_name].append(phone)

    filename = safe_filename("lpr_tomoru_timezones", base_label or "export", ext="zip")
    filepath = unique_filepath(export_dir, filename)
    manifest = io.StringIO(newline="")
    writer = csv.writer(manifest, delimiter=";")
    writer.writerow(["timezone", "local_call_time", "phone_count", "filename"])

    with zipfile.ZipFile(filepath, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for timezone_name in sorted(groups):
            csv_name = f"{timezone_name.replace('/', '_')}.csv"
            phones = groups[timezone_name]
            archive.writestr(csv_name, build_tomoru_csv(phones))
            writer.writerow([timezone_name, local_call_time, len(phones), csv_name])
        archive.writestr("manifest.csv", manifest.getvalue().encode("utf-8-sig"))
        archive.writestr(
            "README.txt",
            (
                "В архиве отдельный CSV для каждого часового пояса.\n"
                f"Создайте отдельную кампанию Tomoru для каждого файла и назначьте запуск на {local_call_time} по местному времени указанного часового пояса.\n"
                "Номера с неизвестным регионом помещены в Europe_Moscow.csv.\n"
            ).encode("utf-8"),
        )
    return filepath
