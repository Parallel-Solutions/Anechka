"""Парсинг карточки контакта Bitrix24 в нормализованные поля."""

from __future__ import annotations

from typing import Any

POST_CUSTOM_FIELD = "UF_CRM_1567587755197"
_KNOWN_PHONE_TYPES = {"MOBILE", "WORK", "HOME"}


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def build_full_name(contact: dict[str, Any]) -> str:
    last = _first(contact, "LAST_NAME", "lastName")
    name = _first(contact, "NAME", "name")
    second = _first(contact, "SECOND_NAME", "secondName")
    parts = [str(p).strip() for p in (last, name, second) if p and str(p).strip()]
    return " ".join(parts)


def normalize_phone_type(value_type: Any) -> str:
    vt = str(value_type or "").strip().upper()
    return vt if vt in _KNOWN_PHONE_TYPES else "OTHER"


def parse_phones(raw_phones: Any) -> list[dict[str, str]]:
    """Из массива PHONE -> [{'value':..., 'value_type': MOBILE|WORK|HOME|OTHER}] без дублей."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw_phones, str):
        val = raw_phones.strip()
        if val:
            return [{"value": val, "value_type": "WORK"}]
        return result
    if not isinstance(raw_phones, list):
        return result
    for ph in raw_phones:
        if isinstance(ph, dict):
            val = _first(ph, "VALUE", "value")
            vt = normalize_phone_type(_first(ph, "VALUE_TYPE", "valueType"))
        else:
            val, vt = ph, "OTHER"
        if not val:
            continue
        val = str(val).strip()
        if not val or val in seen:
            continue
        seen.add(val)
        result.append({"value": val, "value_type": vt})
    return result


def choose_primary_phone(phones: list[dict[str, str]]) -> dict[str, str] | None:
    """Основной: 1) MOBILE; 2) WORK; 3) первый непустой."""
    if not phones:
        return None
    for vt in ("MOBILE", "WORK"):
        for ph in phones:
            if ph["value_type"] == vt:
                return ph
    return phones[0]


def extract_contact_fields(contact: dict[str, Any]) -> dict[str, Any]:
    """Нормализованные поля из карточки crm.contact.get (UPPER) или из полей лида (mixed)."""
    phones = parse_phones(_first(contact, "PHONE", "phone"))
    primary = choose_primary_phone(phones)
    company_id = _first(contact, "COMPANY_ID", "companyId")
    return {
        "last_name": _first(contact, "LAST_NAME", "lastName"),
        "name": _first(contact, "NAME", "name"),
        "second_name": _first(contact, "SECOND_NAME", "secondName"),
        "full_name": build_full_name(contact),
        "post": _first(contact, "POST", "post"),
        "post_custom": _first(contact, POST_CUSTOM_FIELD),
        "company_id": int(company_id) if company_id and str(company_id).lstrip("-").isdigit() else None,
        "company_title": _first(contact, "COMPANY_TITLE", "companyTitle", "TITLE", "title"),
        "phones": phones,
        "primary_phone": primary["value"] if primary else None,
        "primary_phone_type": primary["value_type"] if primary else None,
    }
