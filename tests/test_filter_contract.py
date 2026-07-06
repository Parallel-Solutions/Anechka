"""Contract tests mirroring frontend FILTER_MATCHERS in call_results.js."""

from app.models import CallResultImportRow
from app.services.call_results.row_disposition import get_row_disposition
from app.services.call_results.row_filter import get_row_filter

ROW_FILTER_CATEGORIES = (
    "manual_review",
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


def matches_filter(row: CallResultImportRow, actions: list, filter_id: str) -> bool:
    rf = get_row_filter(row, actions)
    ud = get_row_disposition(row, actions)
    if filter_id == "all":
        return True
    if filter_id == "manual_review":
        return rf == "manual_review"
    if filter_id == "manual_call":
        return rf == "manual_call" or ud == "manual_call"
    if filter_id == "auto_call":
        return rf == "auto_call"
    return rf == filter_id


def test_positive_todo_only_in_new_todos_not_manual_review():
    row = _row(primary_outcome="positive", business_signals={"positive": True})
    actions = [_action("tasks.task.add")]
    assert matches_filter(row, actions, "new_todos")
    assert not matches_filter(row, actions, "manual_review")


def test_contact_action_only_in_new_contacts():
    row = _row()
    actions = [_action("crm.contact.add")]
    assert matches_filter(row, actions, "new_contacts")
    assert not matches_filter(row, actions, "manual_review")


def test_hangup_during_robocall_only_manual_call():
    row = _row(business_signals={"hangup_during_robocall": True})
    assert matches_filter(row, [], "manual_call")
    assert not matches_filter(row, [], "new_comments")
    assert not matches_filter(row, [], "manual_review")


def test_hangup_without_result_comment_in_new_comments():
    row = _row(business_signals={"hangup_without_result": True})
    actions = [_action("crm.timeline.comment.add")]
    assert matches_filter(row, actions, "new_comments")
    assert not matches_filter(row, actions, "manual_call")


def test_pure_no_answer_only_auto_call():
    row = _row(
        primary_outcome="no_answer",
        business_signals={"no_answer": True},
    )
    assert matches_filter(row, [], "auto_call")
    assert not matches_filter(row, [], "manual_review")


def test_needs_manual_review_only_manual_review():
    row = _row(
        needs_manual_review=True,
        business_signals={"explicit_refusal": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert matches_filter(row, actions, "manual_review")
    assert not matches_filter(row, actions, "new_comments")


def test_row_filter_categories_partition_except_manual_call_overlap():
    cases = [
        (_row(needs_manual_review=True), []),
        (_row(primary_outcome="refusal", business_signals={"explicit_refusal": True}), [
            _action("crm.timeline.comment.add"),
        ]),
        (_row(primary_outcome="positive", business_signals={"positive": True}), [
            _action("tasks.task.add"),
        ]),
        (_row(), [_action("crm.contact.add")]),
        (_row(primary_outcome="no_answer", business_signals={"no_answer": True}), []),
        (_row(operator_filter="new_comments"), [_action("crm.activity.todo.add")]),
        (_row(business_signals={"hangup_without_result": True}), [
            _action("crm.timeline.comment.add"),
        ]),
    ]
    for row, actions in cases:
        matched = [f for f in ROW_FILTER_CATEGORIES if matches_filter(row, actions, f)]
        assert len(matched) == 1, (get_row_filter(row, actions), matched)


def test_manual_call_row_filter_only_in_manual_call_filter():
    row = _row(business_signals={"callback_later_requested": True})
    actions = [_action("retry_queue.add", reason="callback_later")]
    assert get_row_filter(row, actions) == "manual_call"
    assert matches_filter(row, actions, "manual_call")
    assert not any(matches_filter(row, actions, f) for f in ROW_FILTER_CATEGORIES)


def test_operator_filter_moves_row_between_filters():
    row = _row(
        operator_filter="new_comments",
        needs_manual_review=True,
        business_signals={"explicit_refusal": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert matches_filter(row, actions, "new_comments")
    assert not matches_filter(row, actions, "manual_review")
