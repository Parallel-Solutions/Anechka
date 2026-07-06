"""Mutually exclusive UI filter bucket for import rows."""

from __future__ import annotations

from typing import Any, Literal

from app.models.call_results import BitrixPreparedAction, CallResultImportRow
from app.services.call_results.row_disposition import (
    _enabled_actions,
    is_manual_review_row,
    row_matches_auto_call,
    row_matches_manual_call,
)

RowFilter = Literal[
    "manual_review",
    "manual_call",
    "auto_call",
    "new_contacts",
    "new_todos",
    "new_comments",
]

PrimaryBucket = Literal["manual_review", "auto_call", "new_comments"]

_ROW_FILTER_TO_PRIMARY: dict[RowFilter, PrimaryBucket] = {
    "auto_call": "auto_call",
    "new_comments": "new_comments",
    "manual_review": "manual_review",
    "manual_call": "manual_review",
    "new_todos": "manual_review",
    "new_contacts": "manual_review",
}

OPERATOR_FILTER_BY_ACTION: dict[str, RowFilter] = {
    "comment": "new_comments",
    "todo": "new_todos",
    "create_contact": "new_contacts",
    "find_contact": "auto_call",
}

KEEP_METHODS_BY_FILTER: dict[RowFilter, frozenset[str]] = {
    "new_comments": frozenset({"crm.timeline.comment.add"}),
    "new_todos": frozenset({"tasks.task.add", "crm.activity.todo.add"}),
    "new_contacts": frozenset({"crm.contact.list", "crm.contact.add", "crm.deal.contact.add"}),
    "auto_call": frozenset({"retry_queue.add"}),
    "manual_review": frozenset(),
    "manual_call": frozenset(),
}


def _has_enabled_method(
    actions: list[BitrixPreparedAction | dict[str, Any]],
    method: str,
) -> bool:
    for action in _enabled_actions(actions):
        action_method = action.method if hasattr(action, "method") else action.get("method")
        if action_method == method:
            return True
    return False


def _has_retry_reason(
    actions: list[BitrixPreparedAction | dict[str, Any]],
    reason: str,
) -> bool:
    for action in _enabled_actions(actions):
        method = action.method if hasattr(action, "method") else action.get("method")
        if method != "retry_queue.add":
            continue
        payload = action.payload if hasattr(action, "payload") else action.get("payload")
        if isinstance(payload, dict) and payload.get("reason") == reason:
            return True
    return False


def _is_alternate_contact_row(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> bool:
    if not (row.business_signals or {}).get("alternate_contact_requested"):
        return False
    return (
        _has_enabled_method(actions, "crm.deal.contact.add")
        or _has_retry_reason(actions, "alternate_contact")
    )


def get_dial_phone(row: CallResultImportRow) -> str | None:
    ext = row.extracted_data or {}
    dial = ext.get("dial_phone")
    if dial:
        return str(dial)
    return row.normalized_phone or row.raw_phone


def get_row_filter(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> RowFilter:
    operator_filter = getattr(row, "operator_filter", None)
    if operator_filter:
        return operator_filter  # type: ignore[return-value]

    if row.needs_manual_review:
        return "manual_review"

    if _has_enabled_method(actions, "crm.contact.add") or _is_alternate_contact_row(row, actions):
        return "new_contacts"
    if _has_enabled_method(actions, "tasks.task.add") or _has_enabled_method(actions, "crm.activity.todo.add"):
        return "new_todos"
    if _has_enabled_method(actions, "crm.timeline.comment.add"):
        return "new_comments"

    if is_manual_review_row(row, actions):
        return "manual_review"

    if row_matches_manual_call(row, actions):
        return "manual_call"
    if row_matches_auto_call(row, actions):
        return "auto_call"

    return "manual_review"


def get_primary_bucket(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> PrimaryBucket:
    return _ROW_FILTER_TO_PRIMARY[get_row_filter(row, actions)]
