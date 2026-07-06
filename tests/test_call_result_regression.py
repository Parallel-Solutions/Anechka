"""Regression tests for forbidden operations."""

from app.models import CallResultImportRow
from app.services.call_results.action_planner import BitrixActionPlanner
from app.services.call_results.deterministic_pre_classifier import DeterministicPreClassifier
from app.services.call_results.llm_schema import CallResultSignals
from app.services.call_results.payload_validator import BitrixPayloadValidator


def test_task_payload_missing_fields_invalid():
    v = BitrixPayloadValidator().validate("tasks.task.add", {})
    assert v.status == "invalid"


def test_archive_forbidden():
    v = BitrixPayloadValidator().validate("bitrix_archive_deal", {})
    assert v.status == "invalid"


def test_stage_change_forbidden():
    v = BitrixPayloadValidator().validate("crm.item.update", {})
    assert v.status == "invalid"


def test_refusal_no_retry():
    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1,
    )
    actions = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1,
        assigned_by_id=1,
        signals=CallResultSignals(explicit_refusal=True, confidence=0.9),
        requires_manual=False,
    )
    assert all(a.operation_type != "retry_queue_add" for a in actions)


def test_manual_review_planner_blocks():
    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=True, execution_status="pending", matched_deal_id=1,
    )
    actions = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1,
        assigned_by_id=1,
        signals=CallResultSignals(needs_manual_review=True, positive=True),
        requires_manual=True,
    )
    assert actions[0].operation_type == "manual_review_required"
    assert not actions[0].is_enabled


def test_voicemail_not_hangup():
    pre = DeterministicPreClassifier().classify({"category": "Voicemail"})
    assert not pre.unsupported_outcome
    assert pre.det_signals and pre.det_signals.no_answer
    assert not pre.det_signals.hangup_without_result


def test_orchestrator_adds_outcome_comment_for_non_auto_call():
    from app.services.call_results.action_planner import BitrixActionPlanner
    from app.services.call_results.row_disposition import get_row_disposition, should_plan_outcome_comment

    row = CallResultImportRow(
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
        business_signals={"callback_later_requested": True},
    )
    planned = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=CallResultSignals(callback_later_requested=True, confidence=0.9),
        requires_manual=False,
    )
    planner = BitrixActionPlanner()
    if should_plan_outcome_comment(row, planned):
        order = max((a.sort_order for a in planned), default=-1) + 1
        planned.append(planner.plan_outcome_comment(sort_order=order))
    assert any(a.method == "crm.timeline.comment.add" for a in planned)
    assert get_row_disposition(row, planned) == "manual_call"
