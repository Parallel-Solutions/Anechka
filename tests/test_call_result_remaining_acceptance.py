"""Regression matrix for the remaining call-result acceptance items."""

from __future__ import annotations

import pytest

from app.models import CallResultImportRow
from app.services.call_results.action_planner import BitrixActionPlanner
from app.services.call_results.deterministic_pre_classifier import (
    DeterministicPreClassifier,
    PreClassResult,
)
from app.services.call_results.llm_schema import CallResultLLMResult, CallResultSignals
from app.services.call_results.signal_merger import SignalMerger


@pytest.mark.parametrize(
    "transcript",
    [
        "Да, предложение интересно, пришлите коммерческое предложение на почту.",
        "Бюджет согласован, перезвоните в понедельник после обеда.",
        "Соедините с директором, его добавочный номер 123.",
        "Мы уже работаем с другим поставщиком и сейчас отказываемся.",
        "Обсудили проект и сроки поставки, решение будет после совещания.",
    ],
)
def test_interrupted_substantive_dialogue_is_not_deterministic_hangup(transcript):
    row = {
        "technical_result": "Interrupted",
        "call_result": "Interrupted",
        "transcript": transcript,
        "has_meaningful_content": True,
    }

    result = DeterministicPreClassifier().classify(row)

    assert result.llm_required
    assert result.det_signals is None or not result.det_signals.hangup_without_result


@pytest.mark.parametrize(
    "transcript",
    [
        "Да, предложение интересно, пришлите коммерческое предложение на почту.",
        "Бюджет согласован, перезвоните в понедельник после обеда.",
        "Обсудили проект и сроки поставки, решение будет после совещания.",
    ],
)
def test_false_llm_hangup_is_removed_for_substantive_dialogue(transcript):
    merged = SignalMerger().merge(
        PreClassResult(category=None, reason="Interrupted", llm_required=True),
        CallResultLLMResult(
            hangup_without_result=True,
            summary="Соединение прервалось",
            confidence=0.95,
        ),
        normalized_data={"has_meaningful_content": True, "transcript": transcript},
    )

    assert not merged.signals.hangup_without_result
    assert merged.signals.needs_manual_review


def _row() -> CallResultImportRow:
    return CallResultImportRow(
        id=1,
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        raw_phone="+79161234567",
        normalized_phone="79161234567",
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


@pytest.mark.parametrize(
    ("business_group", "signals", "requires_manual", "alternate_id", "expected_operations"),
    [
        ("conversation_yes", CallResultSignals(positive=True), False, None, ["bitrix_add_task"]),
        (
            "conversation_no",
            CallResultSignals(explicit_refusal=True),
            False,
            None,
            ["bitrix_add_comment", "retry_queue_add"],
        ),
        ("callback_same", CallResultSignals(callback_later_requested=True), False, None, ["retry_queue_add"]),
        (
            "callback_other",
            CallResultSignals(alternate_contact_requested=True),
            False,
            9001,
            ["bitrix_link_contact_to_deal", "retry_queue_add"],
        ),
        (
            "conversation_unclear",
            CallResultSignals(needs_manual_review=True, manual_review_reason="неясный итог"),
            True,
            None,
            ["manual_review_required"],
        ),
        ("no_answer", CallResultSignals(no_answer=True), False, None, ["retry_queue_add"]),
        (
            "other",
            CallResultSignals(needs_manual_review=True, manual_review_reason="иное"),
            True,
            None,
            ["manual_review_required"],
        ),
    ],
)
def test_all_seven_business_groups_have_expected_action_flow(
    business_group,
    signals,
    requires_manual,
    alternate_id,
    expected_operations,
):
    actions = BitrixActionPlanner().plan(
        _row(),
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=signals,
        requires_manual=requires_manual,
        resolved_alternate_contact_id=alternate_id,
    )

    assert [action.operation_type for action in actions] == expected_operations, business_group
