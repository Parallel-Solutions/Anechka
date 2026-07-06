"""Unit tests for mutually exclusive row UI disposition."""

from app.models import CallResultImportRow
from app.services.call_results.row_disposition import (
    get_row_disposition,
    is_manual_review_row,
    row_matches_auto_call,
    row_matches_manual_call,
    should_plan_outcome_comment,
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
    )
    defaults.update(kw)
    return CallResultImportRow(**defaults)


def _action(method: str, *, reason: str | None = None, enabled: bool = True) -> dict:
    payload = {"reason": reason} if reason else {}
    return {"method": method, "payload": payload, "is_enabled": enabled}


def test_manual_review_blocks_manual_call():
    row = _row(
        needs_manual_review=True,
        business_signals={"callback_later_requested": True},
    )
    actions = [_action("retry_queue.add", reason="callback_later")]
    assert row_matches_manual_call(row, actions)
    assert get_row_disposition(row, actions) == "manual_review"


def test_hangup_without_result_comment_is_manual_review():
    row = _row(business_signals={"hangup_without_result": True})
    actions = [_action("crm.timeline.comment.add")]
    assert not row_matches_manual_call(row, actions)
    assert not row_matches_auto_call(row, actions)
    assert get_row_disposition(row, actions) == "manual_review"


def test_hangup_during_robocall_is_manual_call():
    row = _row(business_signals={"hangup_during_robocall": True})
    assert row_matches_manual_call(row, [])
    assert not row_matches_auto_call(row, [])
    assert get_row_disposition(row, []) == "manual_call"


def test_pure_no_answer_is_auto_call():
    row = _row(
        primary_outcome="no_answer",
        business_signals={"no_answer": True},
    )
    assert get_row_disposition(row, []) == "auto_call"


def test_refusal_not_found_is_manual_review():
    row = _row(
        match_status="not_found",
        matched_deal_id=None,
        primary_outcome="refusal",
        business_signals={"explicit_refusal": True},
    )
    assert get_row_disposition(row, []) == "manual_review"
    assert is_manual_review_row(row, [])


def test_callback_later_without_manual_review_flag_is_manual_call():
    row = _row(
        business_signals={"callback_later_requested": True},
    )
    actions = [_action("retry_queue.add", reason="callback_later")]
    assert get_row_disposition(row, actions) == "manual_call"


def test_refusal_matched_is_manual_review_without_call_actions():
    row = _row(
        primary_outcome="refusal",
        business_signals={"explicit_refusal": True},
    )
    actions = [_action("crm.timeline.comment.add")]
    assert get_row_disposition(row, actions) == "manual_review"


def test_dispositions_are_mutually_exclusive():
    cases = [
        (_row(needs_manual_review=True, business_signals={"callback_later_requested": True}), [
            _action("retry_queue.add", reason="callback_later"),
        ]),
        (_row(business_signals={"hangup_without_result": True}), [
            _action("crm.timeline.comment.add"),
        ]),
        (_row(primary_outcome="no_answer", business_signals={"no_answer": True}), []),
        (_row(match_status="not_found", matched_deal_id=None, primary_outcome="refusal",
              business_signals={"explicit_refusal": True}), []),
        (_row(business_signals={"callback_later_requested": True}), [
            _action("retry_queue.add", reason="callback_later"),
        ]),
    ]
    for row, actions in cases:
        flags = [
            get_row_disposition(row, actions) == "manual_review",
            get_row_disposition(row, actions) == "manual_call",
            get_row_disposition(row, actions) == "auto_call",
        ]
        assert sum(flags) <= 1


def test_should_plan_outcome_comment_for_manual_call():
    row = _row(business_signals={"callback_later_requested": True})
    actions = [_action("retry_queue.add", reason="callback_later")]
    assert should_plan_outcome_comment(row, actions)


def test_should_not_plan_outcome_comment_for_auto_call():
    row = _row(primary_outcome="no_answer", business_signals={"no_answer": True})
    actions = [_action("retry_queue.add", reason="no_answer")]
    assert not should_plan_outcome_comment(row, actions)


def test_should_not_plan_outcome_comment_without_deal():
    row = _row(
        match_status="not_found",
        matched_deal_id=None,
        primary_outcome="refusal",
        business_signals={"explicit_refusal": True},
    )
    assert not should_plan_outcome_comment(row, [])


def test_should_not_plan_outcome_comment_when_already_present():
    row = _row(primary_outcome="refusal", business_signals={"explicit_refusal": True})
    actions = [_action("crm.timeline.comment.add")]
    assert not should_plan_outcome_comment(row, actions)


def test_should_not_plan_outcome_comment_for_positive():
    row = _row(primary_outcome="positive", business_signals={"positive": True})
    actions = [_action("tasks.task.add")]
    assert not should_plan_outcome_comment(row, actions)


def test_should_not_plan_outcome_comment_for_alternate_contact():
    row = _row(
        primary_outcome="alternate_contact",
        business_signals={"alternate_contact_requested": True},
    )
    actions = [
        _action("crm.deal.contact.add"),
        _action("retry_queue.add", reason="alternate_contact"),
    ]
    assert not should_plan_outcome_comment(row, actions)


def test_should_not_plan_outcome_comment_for_hangup_without_result():
    row = _row(business_signals={"hangup_without_result": True})
    actions = [_action("crm.timeline.comment.add")]
    assert not should_plan_outcome_comment(row, actions)
