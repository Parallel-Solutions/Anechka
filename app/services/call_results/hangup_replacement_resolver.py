"""Resolve replacement LPR contact for hangup-without-answer rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CallResultImportRow
from app.services.call_results.action_planner import PlannedAction
from app.services.call_results.lpr_contact_search_provider import (
    LprContactSearchError,
    LprContactSearchProvider,
)


@dataclass
class HangupReplacementLookup:
    found: dict[str, Any] | None = None
    error_message: str | None = None

    @property
    def failed(self) -> bool:
        return self.error_message is not None


def lookup_hangup_replacement_contact(
    lpr_search: LprContactSearchProvider,
    row: CallResultImportRow,
) -> HangupReplacementLookup:
    if not row.matched_deal_id:
        return HangupReplacementLookup(error_message="Сделка не найдена — невозможен поиск другого контакта")
    try:
        found = lpr_search.find_replacement_contact(
            deal_id=int(row.matched_deal_id),
            exclude_phone=row.normalized_phone or row.raw_phone,
            exclude_contact_id=row.matched_contact_id,
        )
    except LprContactSearchError as exc:
        return HangupReplacementLookup(error_message=str(exc))
    if found is None:
        return HangupReplacementLookup(
            error_message="Не удалось найти другой контакт по правилам ЛПР",
        )
    return HangupReplacementLookup(found=found)


def build_lpr_search_provider(
    db: Session,
    portal_id: str,
    settings: Settings,
) -> LprContactSearchProvider:
    return LprContactSearchProvider(db, portal_id, settings)


def apply_hangup_replacement_success(
    row: CallResultImportRow,
    planned: list[PlannedAction],
    found: dict[str, Any],
) -> list[PlannedAction]:
    ext = dict(row.extracted_data or {})
    ext["dial_phone"] = found["phone"]
    ext["replacement_contact_id"] = found["contact_id"]
    row.extracted_data = ext

    updated: list[PlannedAction] = []
    for action in planned:
        if action.method == "contact_search.add":
            continue
        if action.method == "retry_queue.add":
            action.payload = {
                **action.payload,
                "reason": "hangup_replacement_contact",
                "search_required": False,
            }
            action.human_summary = "Перезвон на другой номер"
        updated.append(action)
    return updated
