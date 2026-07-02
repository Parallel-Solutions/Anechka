"""Unit tests for mutually exclusive row UI disposition."""

from app.models import CallResultImportRow
from app.services.call_results.row_disposition import (
    get_row_disposition,
    is_manual_review_row,
    row_matches_auto_call,
    row_matches_manual_call,
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


def test_hangup_goes_to_manual_call_not_auto():
    row = _row(business_signals={"hangup_without_result": True})
    actions = [
        _action("contact_search.add"),
        _action("retry_queue.add", reason="hangup_replacement_contact"),
    ]
    assert row_matches_manual_call(row, actions)
    assert row_matches_auto_call(row, actions)
    assert get_row_disposition(row, actions) == "manual_call"


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
            _action("contact_search.add"),
            _action("retry_queue.add", reason="hangup_replacement_contact"),
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
