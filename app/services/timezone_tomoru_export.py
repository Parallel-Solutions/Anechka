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


def _campaign_name(timezone_name: str, local_call_time: str) -> str:
    safe_timezone = timezone_name.replace("/", "_")
    safe_time = local_call_time.replace(":", "-")
    return f"Анечка_{safe_timezone}_{safe_time}"


def _build_manual_readme(
    groups: dict[str, list[str]],
    local_call_time: str,
) -> bytes:
    total_phones = sum(len(phones) for phones in groups.values())
    lines = [
        "РУЧНОЙ ЗАПУСК КАМПАНИЙ В TOMORU",
        "",
        "Анечка подготовила отдельный CSV для каждого часового пояса.",
        "Кампании в Tomoru автоматически не создаются и не запускаются.",
        "",
        f"Всего кампаний: {len(groups)}",
        f"Всего номеров: {total_phones}",
        f"Время звонка для получателя: {local_call_time}",
        "",
        "ЧТО ДЕЛАТЬ:",
        "1. Откройте manifest.csv и обрабатывайте строки по порядку.",
        "2. В Tomoru нажмите «Новая кампания».",
        "3. Скопируйте название из колонки campaign_name.",
        "4. Выберите рабочего агента, но пока не запускайте кампанию.",
        "5. Укажите часовой пояс получателя из колонки timezone.",
        f"6. Установите начало обзвона на {local_call_time} по местному времени получателя.",
        "7. Загрузите CSV из колонки filename.",
        "8. В предпросмотре проверьте количество номеров, агента, дату, время и часовой пояс.",
        "9. Запускайте кампанию только после проверки всех параметров.",
        "",
        "ФАЙЛЫ:",
    ]
    for index, timezone_name in enumerate(sorted(groups), start=1):
        csv_name = f"{timezone_name.replace('/', '_')}.csv"
        campaign_name = _campaign_name(timezone_name, local_call_time)
        lines.append(
            f"{index}. {csv_name} — {len(groups[timezone_name])} номеров; "
            f"часовой пояс {timezone_name}; запуск {local_call_time}; "
            f"кампания {campaign_name}."
        )
    lines.extend(
        [
            "",
            "ВАЖНО:",
            "Номера, для которых часовой пояс определить не удалось, попадают в Europe_Moscow.csv.",
            "Перед запуском такой кампании проверьте номера вручную.",
        ]
    )
    return ("\n".join(lines) + "\n").encode("utf-8-sig")


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
    writer.writerow(
        ["order", "timezone", "local_call_time", "phone_count", "filename", "campaign_name"]
    )

    with zipfile.ZipFile(filepath, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, timezone_name in enumerate(sorted(groups), start=1):
            csv_name = f"{timezone_name.replace('/', '_')}.csv"
            phones = groups[timezone_name]
            archive.writestr(csv_name, build_tomoru_csv(phones))
            writer.writerow(
                [
                    index,
                    timezone_name,
                    local_call_time,
                    len(phones),
                    csv_name,
                    _campaign_name(timezone_name, local_call_time),
                ]
            )
        archive.writestr("manifest.csv", manifest.getvalue().encode("utf-8-sig"))
        archive.writestr("README.txt", _build_manual_readme(groups, local_call_time))
    return filepath
