"""Column alias mapping for call result files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

FIELD_ALIASES: dict[str, list[str]] = {
    "phone": [
        "телефон", "номер", "номер телефона", "phone", "phone_number", "client_phone",
    ],
    "region": ["регион", "region"],
    "category": [
        "категория", "статус", "результат", "категория результата",
        "result", "result_category", "call_result",
    ],
    "contact_name": [
        "контакт", "фio", "фio", "фио", "контактное лицо", "contact", "contact_name",
    ],
    "comment": [
        "комментарий", "итог", "результат разговора", "содержание",
        "comment", "summary", "call_summary",
    ],
    "called_at": ["дата звонка", "время звонка", "call_date", "called_at"],
    "callback_at": [
        "срок", "тайминг", "перезвонить", "дата перезвона", "callback_at", "deadline",
    ],
    "call_id": ["call_id"],
    "campaign_id": ["campaign_id"],
    "recording_url": ["recording_url"],
    "transcript": ["transcript", "расшифровка", "транскрипт"],
    "deal_id": ["deal_id"],
    "contact_id": ["contact_id"],
    "responsible_id": ["responsible_id"],
    "email": ["email", "e-mail", "почта"],
    "extension": ["добавочный номер", "extension", "доб"],
    "technical_result": ["technical_result", "технический статус", "статус звонка"],
    "scenario_answers": ["scenario_answers", "ответы сценария"],
}


def _normalize_header(h: str) -> str:
    return re.sub(r"[\s_\-]+", " ", h.strip().lower())


@dataclass
class ColumnMappingResult:
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped_headers: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    needs_manual: bool = False
    error: str | None = None


class CallResultColumnMapper:
    def map_headers(self, headers: list[str], user_mapping: dict[str, str] | None = None) -> ColumnMappingResult:
        if user_mapping:
            return ColumnMappingResult(mapping=dict(user_mapping))

        norm_headers = {_normalize_header(h): h for h in headers if h and str(h).strip()}
        result = ColumnMappingResult()
        used: set[str] = set()

        for field_name, aliases in FIELD_ALIASES.items():
            matches = []
            for alias in aliases:
                na = _normalize_header(alias)
                if na in norm_headers:
                    matches.append(norm_headers[na])
            if len(matches) == 1:
                result.mapping[field_name] = matches[0]
                used.add(_normalize_header(matches[0]))
            elif len(matches) > 1:
                result.ambiguous[field_name] = matches

        for nh, orig in norm_headers.items():
            if nh not in used and orig not in result.mapping.values():
                result.unmapped_headers.append(orig)

        if "phone" not in result.mapping:
            result.needs_manual = True
            result.error = "Не найдена колонка с телефоном"

        if result.ambiguous:
            result.needs_manual = True

        return result

    def apply_mapping(self, row: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field_name, header in mapping.items():
            if header in row:
                val = row[header]
                if val is not None and str(val).strip() != "":
                    out[field_name] = val
        return out
