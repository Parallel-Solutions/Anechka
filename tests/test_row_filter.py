"""Unit tests for mutually exclusive row UI filters."""

from app.models import CallResultImportRow
from app.services.call_results.row_filter import get_dial_phone, get_row_filter

ALL_FILTERS = (
    "manual_review",
    "manual_call",
    "auto_call",
    "new_contacts",
    "new_todos",
    "new_comments",
)


def _row(**kw) -> CallResultImportRow:
    defaults = dict(
        id=1,
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        match_status="matched",
        llm_status="not_required",
        llm_required=False,
        manually_overridden=False,
        llm_input_truncated=False,
        is_duplicate=False,
        needs_manual_review=False,
        execution_status="pending",
        matched_deal_id=1001,
        operator_filter=None,
    )
    defaults.update(kw)
    return CallResultImportRow(**defaults)


def _action(method: str, *, reason: str | None = None, enabled: bool = True) -> dict:
    payload = {"reason": reason} if reason else {}
    return {"method": method, "payload": payload, "is_enabled": enabled}


def _matches_filter(row: CallResultImportRow, actions: list, filter_id: str) -> bool:
    return get_row_filter(row, actions) == filter_id


def test_operator_filter_overrides_computed_bucket():
    row = _row(
        operator_filter="auto_call",
        needs_manual_review=True,
        business_signals={"callback_later_requested": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert get_row_filter(row, actions) == "auto_call"


def test_comment_action_goes_to_new_comments_not_manual_review():
    row = _row(
        primary_outcome="refusal",
        business_signals={"explicit_refusal": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert get_row_filter(row, actions) == "new_comments"


def test_todo_action_goes_to_new_todos():
    row = _row(primary_outcome="positive", business_signals={"positive": True})
    actions = [_action("crm.activity.todo.add")]
    assert get_row_filter(row, actions) == "new_todos"


def test_contact_action_goes_to_new_contacts():
    row = _row()
    actions = [
        _action("crm.contact.list"),
        _action("crm.contact.add"),
        _action("crm.deal.contact.add"),
    ]
    assert get_row_filter(row, actions) == "new_contacts"


def test_pure_no_answer_is_auto_call():
    row = _row(
        primary_outcome="no_answer",
        business_signals={"no_answer": True},
    )
    assert get_row_filter(row, []) == "auto_call"


def test_callback_later_is_manual_call():
    row = _row(business_signals={"callback_later_requested": True})
    actions = [_action("retry_queue.add", reason="callback_later")]
    assert get_row_filter(row, actions) == "manual_call"


def test_hangup_with_contact_search_is_manual_call():
    row = _row(business_signals={"hangup_without_result": True})
    actions = [
        _action("contact_search.add"),
        _action("retry_queue.add", reason="hangup_replacement_contact"),
    ]
    assert get_row_filter(row, actions) == "manual_call"


def test_needs_manual_review_wins_over_actions():
    row = _row(
        needs_manual_review=True,
        business_signals={"explicit_refusal": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert get_row_filter(row, actions) == "manual_review"


def test_row_filters_are_mutually_exclusive():
    cases = [
        (_row(needs_manual_review=True), []),
        (_row(primary_outcome="refusal", business_signals={"explicit_refusal": True}), [
            _action("crm.timeline.comment.add"),
        ]),
        (_row(primary_outcome="positive", business_signals={"positive": True}), [
            _action("crm.activity.todo.add"),
        ]),
        (_row(), [_action("crm.contact.add")]),
        (_row(business_signals={"callback_later_requested": True}), [
            _action("retry_queue.add", reason="callback_later"),
        ]),
        (_row(primary_outcome="no_answer", business_signals={"no_answer": True}), []),
        (_row(operator_filter="new_comments"), [_action("crm.activity.todo.add")]),
    ]
    for row, actions in cases:
        bucket = get_row_filter(row, actions)
        assert bucket in ALL_FILTERS
        matched = [f for f in ALL_FILTERS if _matches_filter(row, actions, f)]
        assert matched == [bucket]


def test_get_dial_phone_prefers_extracted_dial_phone():
    row = _row(
        normalized_phone="89161234567",
        extracted_data={"dial_phone": "89169999999"},
    )
    assert get_dial_phone(row) == "89169999999"


def test_get_dial_phone_falls_back_to_row_phone():
    row = _row(normalized_phone="89161234567")
    assert get_dial_phone(row) == "89161234567"
