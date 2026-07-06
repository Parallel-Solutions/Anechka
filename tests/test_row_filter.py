"""Unit tests for mutually exclusive row UI filters."""

from app.models import CallResultImportRow
from app.services.call_results.row_disposition import get_row_disposition
from app.services.call_results.row_filter import get_dial_phone, get_primary_bucket, get_row_filter

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
    actions = [_action("tasks.task.add")]
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


def test_hangup_without_result_comment_is_new_comments():
    row = _row(business_signals={"hangup_without_result": True})
    actions = [_action("crm.timeline.comment.add")]
    assert get_row_filter(row, actions) == "new_comments"
    assert get_row_disposition(row, actions) == "manual_review"


def test_hangup_during_robocall_is_manual_call_without_comment():
    row = _row(business_signals={"hangup_during_robocall": True})
    assert get_row_filter(row, []) == "manual_call"
    assert get_row_disposition(row, []) == "manual_call"


def test_needs_manual_review_wins_over_actions():
    row = _row(
        needs_manual_review=True,
        business_signals={"explicit_refusal": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert get_row_filter(row, actions) == "manual_review"


def test_alternate_contact_found_is_new_contacts():
    row = _row(business_signals={"alternate_contact_requested": True})
    actions = [
        _action("crm.deal.contact.add"),
        _action("retry_queue.add", reason="alternate_contact"),
    ]
    assert get_row_filter(row, actions) == "new_contacts"


def test_row_filters_are_mutually_exclusive():
    cases = [
        (_row(needs_manual_review=True), []),
        (_row(primary_outcome="refusal", business_signals={"explicit_refusal": True}), [
            _action("crm.timeline.comment.add"),
        ]),
        (_row(primary_outcome="positive", business_signals={"positive": True}), [
            _action("tasks.task.add"),
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


def test_primary_bucket_mapping():
    cases = [
        (_row(primary_outcome="refusal", business_signals={"explicit_refusal": True}), [
            _action("crm.timeline.comment.add"),
        ], "new_comments"),
        (_row(primary_outcome="positive", business_signals={"positive": True}), [
            _action("tasks.task.add"),
        ], "manual_review"),
        (_row(), [_action("crm.contact.add")], "manual_review"),
        (_row(business_signals={"callback_later_requested": True}), [
            _action("retry_queue.add", reason="callback_later"),
        ], "manual_review"),
        (_row(primary_outcome="no_answer", business_signals={"no_answer": True}), [], "auto_call"),
        (_row(needs_manual_review=True), [], "manual_review"),
    ]
    for row, actions, expected in cases:
        assert get_primary_bucket(row, actions) == expected


def test_positive_goes_to_primary_manual_review():
    row = _row(primary_outcome="positive", business_signals={"positive": True})
    actions = [_action("tasks.task.add")]
    assert get_row_filter(row, actions) == "new_todos"
    assert get_primary_bucket(row, actions) == "manual_review"


def test_refusal_goes_to_primary_new_comments():
    row = _row(primary_outcome="refusal", business_signals={"explicit_refusal": True})
    actions = [_action("crm.timeline.comment.add")]
    assert get_primary_bucket(row, actions) == "new_comments"


def test_primary_buckets_sum_to_total():
    cases = [
        (_row(needs_manual_review=True), []),
        (_row(primary_outcome="refusal", business_signals={"explicit_refusal": True}), [
            _action("crm.timeline.comment.add"),
        ]),
        (_row(primary_outcome="positive", business_signals={"positive": True}), [
            _action("tasks.task.add"),
        ]),
        (_row(), [_action("crm.contact.add")]),
        (_row(business_signals={"callback_later_requested": True}), [
            _action("retry_queue.add", reason="callback_later"),
        ]),
        (_row(primary_outcome="no_answer", business_signals={"no_answer": True}), []),
        (_row(operator_filter="new_comments"), [_action("crm.activity.todo.add")]),
    ]
    counts = {"manual_review": 0, "auto_call": 0, "new_comments": 0}
    for row, actions in cases:
        bucket = get_primary_bucket(row, actions)
        counts[bucket] += 1
    assert sum(counts.values()) == len(cases)
