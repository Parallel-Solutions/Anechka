"""Build a Tomoru phone CSV from user supplied CSV/XLSX files."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any

from app.services.call_results.file_parser import CallResultFileParser, ParsedSheet
from app.services.call_results.phone_normalizer import parse_phones


PHONE_HEADER_ALIASES = {
    "phone",
    "phone number",
    "phone_number",
    "mobile",
    "mobile phone",
    "telephone",
    "телефон",
    "номер телефона",
    "мобильный",
    "мобильный телефон",
    "номер",
}


@dataclass(frozen=True)
class ExternalPhoneImportResult:
    phones: list[str]
    rows_total: int
    rows_with_phone: int
    invalid_rows: int


def _normalized_header(value: str) -> str:
    text = re.sub(r"[_\-]+", " ", str(value or "").strip().lower())
    return " ".join(text.split())


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _candidate_columns(headers: list[str]) -> list[str]:
    return [header for header in headers if _normalized_header(header) in PHONE_HEADER_ALIASES]


def _parse_csv(content: bytes) -> ParsedSheet:
    text: str | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Не удалось определить кодировку CSV")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [str(value or "").strip() for value in (reader.fieldnames or [])]
    rows = [
        {str(key or "").strip(): value.strip() if isinstance(value, str) else value for key, value in row.items()}
        for row in reader
        if any(value is not None and str(value).strip() for value in row.values())
    ]
    return ParsedSheet(name="CSV", headers=headers, rows=rows)


def extract_external_phones(content: bytes, filename: str) -> ExternalPhoneImportResult:
    if filename.lower().endswith(".csv"):
        sheet = _parse_csv(content)
    else:
        parser = CallResultFileParser()
        parsed = parser.parse(content, filename)
        if parsed.error:
            raise ValueError(parsed.error)
        sheet = parser.get_sheet(parsed, parsed.selected_sheet or "")
        if sheet is None:
            raise ValueError("В файле не найден лист с данными")

    phone_columns = _candidate_columns(sheet.headers)
    seen: set[str] = set()
    phones: list[str] = []
    rows_with_phone = 0
    invalid_rows = 0

    for row in sheet.rows:
        values = [row.get(column) for column in phone_columns] if phone_columns else list(row.values())
        row_phones: list[str] = []
        for value in values:
            row_phones.extend(parse_phones(_cell_text(value)).phones)
        unique_row_phones = list(dict.fromkeys(row_phones))
        if not unique_row_phones:
            invalid_rows += 1
            continue
        rows_with_phone += 1
        for phone in unique_row_phones:
            if phone not in seen:
                seen.add(phone)
                phones.append(phone)

    if not phones:
        raise ValueError("В файле не найдено корректных телефонных номеров")
    return ExternalPhoneImportResult(
        phones=phones,
        rows_total=len(sheet.rows),
        rows_with_phone=rows_with_phone,
        invalid_rows=invalid_rows,
    )


def build_tomoru_csv(phones: list[str]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["phone_number"])
    for phone in phones:
        writer.writerow([phone])
    return output.getvalue().encode("utf-8-sig")
