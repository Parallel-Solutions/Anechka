'''District field helpers for Tomoru deal filtering.'''

from __future__ import annotations

from typing import Any

from app.services.export_plan.payload_keys import payload_lookup


# Verified Bitrix field «Место проведения работ». Other similarly named legacy
# fields contain unrelated values, including whole numbered settlement lists.
TOMORU_DISTRICT_FIELDS = (
    'UF_CRM_5ECE25C46C803',
)


def normalize_district_name(value: Any) -> str:
    '''Return a stable display/filter value for a Bitrix district field.'''
    if value is None:
        return ''
    return ' '.join(str(value).split())


def district_names_from_payload(raw_payload: dict[str, Any] | None) -> list[str]:
    '''Read the verified district field from a deal payload.'''
    raw = raw_payload or {}
    names: list[str] = []
    seen: set[str] = set()
    for field_code in TOMORU_DISTRICT_FIELDS:
        value = payload_lookup(raw, field_code)
        values = value if isinstance(value, list) else [value]
        for item in values:
            name = normalize_district_name(item)
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
    return names
