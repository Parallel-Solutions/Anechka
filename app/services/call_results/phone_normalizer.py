"""Phone parsing with multi-number and extension support."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.services.phone_service import normalize_phone

_EXT_PATTERNS = [
    re.compile(r"(\d[\d\s\-()]{8,})\s*(?:доб\.?|ext\.?|#|добавочн\.?)\s*(\d+)", re.I),
    re.compile(r"(\d[\d\s\-()]{8,})\s*,\s*(\d{1,6})$"),
]

_SPLIT_RE = re.compile(r"[,;/]+")


@dataclass
class ParsedPhones:
    phones: list[str]
    extension: str | None
    raw_value: str
    status: Literal["single", "multiple", "invalid"]


@dataclass
class ParsedPhone:
    raw: str
    normalized: str | None
    extension: str | None
    is_valid: bool
    multi_status: Literal["single", "multiple", "invalid"] = "single"


def parse_phones(raw: str | None) -> ParsedPhones:
    if not raw or not str(raw).strip():
        return ParsedPhones(phones=[], extension=None, raw_value=str(raw or ""), status="invalid")

    text = str(raw).strip()
    extension: str | None = None
    main_part = text

    for pat in _EXT_PATTERNS:
        m = pat.search(text)
        if m:
            main_part = m.group(1)
            extension = m.group(2)
            break

    segments = [s.strip() for s in _SPLIT_RE.split(main_part) if s.strip()]
    if not segments:
        segments = [main_part]

    normalized_list: list[str] = []
    for seg in segments:
        n = normalize_phone(seg)
        if n:
            normalized_list.append(n)

    if len(normalized_list) > 1:
        return ParsedPhones(phones=normalized_list, extension=extension, raw_value=text, status="multiple")

    if len(normalized_list) == 1:
        return ParsedPhones(phones=normalized_list, extension=extension, raw_value=text, status="single")

    # Short digit sequence without main phone — possible extension only
    digits_only = re.sub(r"\D", "", main_part)
    if 3 <= len(digits_only) <= 6:
        return ParsedPhones(phones=[], extension=extension or digits_only, raw_value=text, status="invalid")

    return ParsedPhones(phones=[], extension=extension, raw_value=text, status="invalid")


def parse_phone_with_extension(raw: str | None) -> ParsedPhone:
    parsed = parse_phones(raw)
    if parsed.status == "single":
        return ParsedPhone(
            raw=parsed.raw_value,
            normalized=parsed.phones[0],
            extension=parsed.extension,
            is_valid=True,
            multi_status="single",
        )
    if parsed.status == "multiple":
        return ParsedPhone(
            raw=parsed.raw_value,
            normalized=None,
            extension=parsed.extension,
            is_valid=False,
            multi_status="multiple",
        )
    return ParsedPhone(
        raw=parsed.raw_value,
        normalized=None,
        extension=parsed.extension,
        is_valid=False,
        multi_status="invalid",
    )
