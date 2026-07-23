"""Map technical call-result signals to the seven business-facing groups."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from app.models.call_results import CallResultImportRow

BusinessGroupCode = Literal[
    "conversation_yes",
    "conversation_no",
    "callback_same",
    "callback_other",
    "conversation_unclear",
    "no_answer",
    "other",
]

BUSINESS_GROUP_LABELS: dict[BusinessGroupCode, str] = {
    "conversation_yes": "РАЗГОВОР БЫЛ, ДА (Есть интерес)",
    "conversation_no": "РАЗГОВОР БЫЛ, НЕТ (отказ)",
    "callback_same": "РАЗГОВОР БЫЛ, ПЕРЕЗВОНИТЬ СЮДА",
    "callback_other": "РАЗГОВОР БЫЛ, ПЕРЕЗВОНИТЬ ДРУГОМУ",
    "conversation_unclear": "РАЗГОВОР БЫЛ, НЕЯСНО",
    "no_answer": "НЕДОЗВОН",
    "other": "ИНОЕ",
}


def get_business_group_code(row: CallResultImportRow) -> BusinessGroupCode:
    signals = row.business_signals or {}
    normalized = row.normalized_data or {}
    primary_outcome = row.primary_outcome or ""

    conversational = (
        "positive",
        "alternate_contact_requested",
        "callback_later_requested",
        "explicit_refusal",
    )
    active_conversational = sum(bool(signals.get(name)) for name in conversational)
    is_mixed = primary_outcome == "mixed" or active_conversational > 1
    if is_mixed:
        return "conversation_unclear"

    if signals.get("alternate_contact_requested"):
        return "callback_other"
    if signals.get("callback_later_requested"):
        return "callback_same"
    if signals.get("positive") or primary_outcome == "positive":
        return "conversation_yes"
    if signals.get("explicit_refusal") or primary_outcome == "refusal":
        return "conversation_no"
    if signals.get("no_answer") or primary_outcome == "no_answer":
        return "no_answer"

    has_meaningful_content = bool(
        normalized.get("has_meaningful_content")
        or signals.get("summary")
        or active_conversational
    )
    if has_meaningful_content:
        return "conversation_unclear"
    return "other"


def count_business_groups(rows: list[CallResultImportRow]) -> dict[str, int]:
    counts = {code: 0 for code in BUSINESS_GROUP_LABELS}
    counts.update(Counter(get_business_group_code(row) for row in rows))
    return counts


def get_business_group(row: CallResultImportRow) -> tuple[BusinessGroupCode, str]:
    code = get_business_group_code(row)
    return code, BUSINESS_GROUP_LABELS[code]
