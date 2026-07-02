"""Mutually exclusive UI disposition for import rows (manual review / manual call / auto call)."""

from __future__ import annotations

from typing import Any, Literal

from app.models.call_results import BitrixPreparedAction, CallResultImportRow

AUTO_RETRY_REASONS = frozenset({"no_answer", "alternate_contact", "hangup_replacement_contact"})

UiDisposition = Literal["manual_review", "manual_call", "auto_call"]


def _enabled_actions(actions: list[BitrixPreparedAction | dict[str, Any]]) -> list[Any]:
    return [a for a in actions if _is_enabled(a)]


def _is_enabled(action: BitrixPreparedAction | dict[str, Any]) -> bool:
    if isinstance(action, dict):
        return action.get("is_enabled") is not False
    return action.is_enabled is not False


def _get_retry_action(actions: list[Any]) -> Any | None:
    for action in actions:
        method = action.method if hasattr(action, "method") else action.get("method")
        if method == "retry_queue.add" and _is_enabled(action):
            return action
    return None


def _action_payload(action: Any) -> dict[str, Any]:
    payload = action.payload if hasattr(action, "payload") else action.get("payload")
    return payload if isinstance(payload, dict) else {}


def is_pure_no_answer_row(row: CallResultImportRow) -> bool:
    sig = row.business_signals or {}
    return bool(
        row.primary_outcome == "no_answer"
        and sig.get("no_answer")
        and not sig.get("callback_later_requested")
        and not sig.get("alternate_contact_requested")
    )


def row_matches_manual_call(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> bool:
    sig = row.business_signals or {}
    enabled = _enabled_actions(actions)
    if any(
        (a.method if hasattr(a, "method") else a.get("method")) == "contact_search.add"
        for a in enabled
    ):
        return True
    retry = _get_retry_action(actions)
    if retry and _action_payload(retry).get("reason") == "callback_later":
        return True
    return bool(sig.get("callback_later_requested"))


def row_matches_auto_call(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> bool:
    retry = _get_retry_action(actions)
    if retry:
        reason = _action_payload(retry).get("reason")
        return reason in AUTO_RETRY_REASONS
    return is_pure_no_answer_row(row)


def is_manual_review_row(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> bool:
    if row.needs_manual_review:
        return True
    return not row_matches_manual_call(row, actions) and not row_matches_auto_call(row, actions)


def get_row_disposition(
    row: CallResultImportRow,
    actions: list[BitrixPreparedAction | dict[str, Any]],
) -> UiDisposition | None:
    if is_manual_review_row(row, actions):
        return "manual_review"
    if row_matches_manual_call(row, actions):
        return "manual_call"
    if row_matches_auto_call(row, actions):
        return "auto_call"
    return None
