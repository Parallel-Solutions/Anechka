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

OPERATOR_FILTER_BY_ACTION: dict[str, RowFilter] = {
    "comment": "new_comments",
    "todo": "new_todos",
    "create_contact": "new_contacts",
    "find_contact": "auto_call",
}

KEEP_METHODS_BY_FILTER: dict[RowFilter, frozenset[str]] = {
    "new_comments": frozenset({"crm.timeline.comment.add"}),
    "new_todos": frozenset({"crm.activity.todo.add"}),
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

    if _has_enabled_method(actions, "crm.contact.add"):
        return "new_contacts"
    if _has_enabled_method(actions, "crm.activity.todo.add"):
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
