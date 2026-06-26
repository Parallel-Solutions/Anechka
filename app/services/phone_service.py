"""Нормализация и сбор телефонных номеров."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class PhoneSource(str, Enum):
    DEAL_CONTACT = "контакт сделки"
    PRIMARY_CONTACT = "основной контакт"
    COMPANY_CONTACT = "контакт компании"
    COMPANY_PHONE = "телефон компании"


@dataclass
class PhoneEntry:
    raw: str
    normalized: str
    phone_type: str
    source: PhoneSource
    contact_id: int | None = None
    contact_name: str = ""


@dataclass
class DealPhoneData:
    deal_id: int
    phones: list[PhoneEntry] = field(default_factory=list)


def normalize_phone(phone: str) -> str | None:
    if not phone or not str(phone).strip():
        return None
    digits = re.sub(r"\D", "", str(phone))
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) < 10:
        return None
    return digits


def format_display_phone(normalized: str) -> str:
    if normalized.startswith("+"):
        return normalized
    if normalized.startswith("7") and len(normalized) == 11:
        return "+" + normalized
    return normalized


def extract_phones_from_multifield(items: list | None) -> list[tuple[str, str]]:
    if not items:
        return []
    result: list[tuple[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("VALUE", "")
            ptype = item.get("VALUE_TYPE", "") or item.get("TYPE", "")
        else:
            value = str(item)
            ptype = ""
        if value:
            result.append((str(value), str(ptype)))
    return result


def add_phone_entries(
    entries: list[PhoneEntry],
    phones: list[tuple[str, str]],
    source: PhoneSource,
    contact_id: int | None = None,
    contact_name: str = "",
    dedup_within_contact: bool = True,
) -> None:
    seen: set[str] = set()
    if dedup_within_contact:
        seen = {e.normalized for e in entries if e.contact_id == contact_id and contact_id is not None}
        if contact_id is None:
            seen = {e.normalized for e in entries if e.source == source}
    for raw, ptype in phones:
        normalized = normalize_phone(raw)
        if not normalized:
            continue
        if dedup_within_contact and normalized in seen:
            continue
        seen.add(normalized)
        entries.append(
            PhoneEntry(
                raw=raw,
                normalized=normalized,
                phone_type=ptype,
                source=source,
                contact_id=contact_id,
                contact_name=contact_name,
            )
        )


def dedup_phones_for_wide(entries: list[PhoneEntry]) -> list[PhoneEntry]:
    """Один номер на сделку в широком формате; ФИО объединяются позже."""
    by_norm: dict[str, PhoneEntry] = {}
    for entry in entries:
        existing = by_norm.get(entry.normalized)
        if existing is None:
            by_norm[entry.normalized] = PhoneEntry(
                raw=entry.raw,
                normalized=entry.normalized,
                phone_type=entry.phone_type,
                source=entry.source,
                contact_id=entry.contact_id,
                contact_name=entry.contact_name,
            )
        else:
            names = {n.strip() for n in [existing.contact_name, entry.contact_name] if n.strip()}
            existing.contact_name = ", ".join(sorted(names))
    return list(by_norm.values())
