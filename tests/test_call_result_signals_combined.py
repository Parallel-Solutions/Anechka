"""Combined signal and regression tests."""

from app.models import CallResultImportRow
from app.services.call_results.action_planner import BitrixActionPlanner
from app.services.call_results.deterministic_pre_classifier import DeterministicPreClassifier, PreClassResult
from app.services.call_results.fake_classifier import positive_result, callback_later_result
from app.services.call_results.llm_schema import (
    CallResultLLMResult,
    CallResultSignals,
    EvidenceItem,
    is_pure_no_answer,
    is_pure_refusal,
)
from app.services.call_results.signal_merger import SignalMerger


def test_positive_plus_callback_later():
    sig = CallResultSignals(positive=True, callback_later_requested=True, callback_text="завтра", confidence=0.9)
    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1,
    )
    actions = BitrixActionPlanner().plan(row, bitrix_deal_id=1, assigned_by_id=1, signals=sig, requires_manual=False)
    ops = {a.operation_type for a in actions}
    assert "bitrix_add_task" in ops
    assert "retry_queue_add" in ops


def test_refusal_plans_delayed_retry():
    sig = CallResultSignals(explicit_refusal=True, confidence=0.9)
    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1,
    )
    actions = BitrixActionPlanner().plan(row, bitrix_deal_id=1, assigned_by_id=1, signals=sig, requires_manual=False)
    retry = next(a for a in actions if a.operation_type == "retry_queue_add")
    assert retry.payload["reason"] == "refusal_followup"


def test_no_answer_not_hangup():
    pre = DeterministicPreClassifier().classify({"category": "No Answer"})
    assert pre.det_signals and pre.det_signals.no_answer
    assert not pre.det_signals.hangup_without_result
    assert not pre.unsupported_outcome


def test_no_answer_merged_to_retry_planner():
    pre = DeterministicPreClassifier().classify({"category": "No Answer"})
    merged = SignalMerger().merge(pre, None, llm_valid=False)
    assert merged.signals.no_answer
    assert merged.primary_outcome == "no_answer"
    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1,
    )
    actions = BitrixActionPlanner().plan(
        row, bitrix_deal_id=1, assigned_by_id=1, signals=merged.signals, requires_manual=False,
    )
    assert any(a.operation_type == "retry_queue_add" for a in actions)
    retry = next(a for a in actions if a.operation_type == "retry_queue_add")
    assert retry.payload.get("reason") == "no_answer"


def test_no_answer_preserved_when_deal_not_found():
    pre = DeterministicPreClassifier().classify({"category": "No Answer"})
    merged = SignalMerger().merge(
        pre,
        None,
        llm_valid=False,
        match_requires_manual=True,
        match_status="not_found",
        match_reason="Телефон не найден",
    )
    assert merged.signals.no_answer
    assert merged.signals.deal_not_found
    assert merged.signals.signal_reasons.get("deal_not_found") == "Телефон не найден"
    assert merged.primary_outcome == "no_answer"
    assert not merged.requires_manual


def test_no_answer_not_in_retry_when_not_found():
    pre = DeterministicPreClassifier().classify({"category": "No Answer"})
    merged = SignalMerger().merge(
        pre,
        None,
        llm_valid=False,
        match_requires_manual=True,
        match_status="not_found",
        match_reason="Телефон не найден",
    )
    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="not_found", match_reason="Телефон не найден",
        llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=None,
    )
    actions = BitrixActionPlanner().plan(
        row, bitrix_deal_id=None, assigned_by_id=None, signals=merged.signals, requires_manual=False,
    )
    assert actions == []


def test_interrupted_with_positive_text():
    llm = positive_result()
    pre = PreClassResult(category=None, reason="Interrupted", llm_required=True)
    merged = SignalMerger().merge(pre, llm, llm_valid=True)
    assert merged.signals.positive


def test_substantive_dialogue_suppresses_false_hangup_from_llm():
    pre = PreClassResult(category=None, reason="Interrupted", llm_required=True)
    llm = CallResultLLMResult(
        hangup_without_result=True,
        summary="Сброс трубки",
        confidence=0.95,
    )
    normalized = {
        "has_meaningful_content": True,
        "transcript": "Мы занимаемся проектом, позвоните после согласования бюджета",
    }

    merged = SignalMerger().merge(pre, llm, normalized_data=normalized)

    assert not merged.signals.hangup_without_result
    assert merged.signals.needs_manual_review
    assert merged.primary_outcome == "manual_review"


def test_substantive_positive_dialogue_keeps_result_and_drops_hangup():
    pre = PreClassResult(category=None, reason="Interrupted", llm_required=True)
    llm = CallResultLLMResult(
        positive=True,
        hangup_without_result=True,
        summary="Есть интерес, связь прервалась",
        confidence=0.95,
        evidence=[
            EvidenceItem(
                source_field="ТЗ",
                text="Нужна документация для закупки",
            )
        ],
    )
    normalized = {
        "has_meaningful_content": True,
        "scenario_events": [
            {"field": "Вопрос 1", "match": "Да, проект уже согласован"},
        ],
    }

    merged = SignalMerger().merge(pre, llm, normalized_data=normalized)

    assert merged.signals.positive
    assert not merged.signals.hangup_without_result
    assert not merged.requires_manual
    assert merged.primary_outcome == "positive"


def test_conflict_refusal_and_positive():
    sig = CallResultSignals(positive=True, explicit_refusal=True, confidence=0.9)
    conflict = SignalMerger._detect_conflicts(PreClassResult(category=None, reason=""), sig)
    assert conflict is not None


def test_callback_llm_preserved_when_deal_not_found():
    """Regression: Interrupted + LLM callback must not be replaced by empty manual review."""
    llm = callback_later_result(
        confidence=0.95,
        needs_manual_review=False,
        callback_text="на той неделе в понедельник",
    )
    pre = PreClassResult(
        category=None,
        reason="Interrupted с содержательными данными — LLM",
        llm_required=True,
    )
    merged = SignalMerger().merge(
        pre,
        llm,
        llm_valid=True,
        match_requires_manual=True,
        match_status="not_found",
        match_reason="Телефон не найден",
    )
    assert merged.signals.callback_later_requested
    assert merged.signals.deal_not_found
    assert not merged.signals.needs_manual_review
    assert merged.primary_outcome == "callback_later"
    assert not merged.requires_manual


def test_is_pure_no_answer_by_primary_outcome():
    assert is_pure_no_answer(None, primary_outcome="no_answer")


def test_is_pure_no_answer_rejects_mixed():
    sig = CallResultSignals(no_answer=True, callback_later_requested=True)
    assert not is_pure_no_answer(sig, primary_outcome="mixed")


def test_is_pure_no_answer_only_no_answer_signal():
    sig = CallResultSignals(no_answer=True, summary="No Answer", confidence=1.0)
    assert is_pure_no_answer(sig)


def test_is_pure_refusal_by_primary_outcome():
    assert is_pure_refusal(None, primary_outcome="refusal")


def test_is_pure_refusal_rejects_mixed():
    sig = CallResultSignals(explicit_refusal=True, callback_later_requested=True)
    assert not is_pure_refusal(sig, primary_outcome="mixed")


def test_is_pure_refusal_only_refusal_signal():
    sig = CallResultSignals(explicit_refusal=True, confidence=0.9)
    assert is_pure_refusal(sig)


def test_refusal_preserved_when_deal_not_found():
    from app.services.call_results.fake_classifier import refusal_result

    llm = refusal_result()
    pre = PreClassResult(category=None, reason="Interrupted", llm_required=True)
    merged = SignalMerger().merge(
        pre,
        llm,
        llm_valid=True,
        match_requires_manual=True,
        match_status="not_found",
        match_reason="Телефон не найден",
    )
    assert merged.signals.explicit_refusal
    assert merged.signals.deal_not_found
    assert merged.signals.signal_reasons.get("deal_not_found") == "Телефон не найден"
    assert merged.primary_outcome == "refusal"
    assert not merged.requires_manual


def test_refusal_in_manual_review_when_not_found():
    from app.services.call_results.row_disposition import get_row_disposition

    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data={}, normalized_data={},
        match_status="not_found", match_reason="Телефон не найден",
        llm_status="completed", llm_required=True, llm_confidence=0.9,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=None,
        primary_outcome="refusal",
        business_signals={"explicit_refusal": True, "confidence": 0.9},
        final_category="refusal",
    )
    assert get_row_disposition(row, []) == "manual_review"
