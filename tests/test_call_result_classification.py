"""Unit tests for deterministic pre-classifier and LLM gate."""

from app.services.call_results.deterministic_pre_classifier import DeterministicPreClassifier
from app.services.call_results.llm_gate import LlmGate


def test_no_answer_unsupported_not_hangup():
    pre = DeterministicPreClassifier().classify({"category": "No Answer"})
    assert not pre.unsupported_outcome
    assert pre.category == "manager_callback"
    assert pre.det_signals and pre.det_signals.no_answer
    assert not pre.det_signals.hangup_without_result
    assert not LlmGate.needs_llm({}, pre, llm_enabled=True)


def test_voicemail_no_answer_signal():
    pre = DeterministicPreClassifier().classify({"call_result": "Voicemail"})
    assert not pre.unsupported_outcome
    assert pre.det_signals and pre.det_signals.no_answer


def test_interrupted_with_transcript_needs_llm():
    row = {"technical_result": "Interrupted", "transcript": "Архитектор будет после 15 июля"}
    pre = DeterministicPreClassifier().classify(row)
    assert pre.llm_required
    assert LlmGate.needs_llm(row, pre, llm_enabled=True)


def test_duplicate_manual_review():
    pre = DeterministicPreClassifier().classify({}, is_duplicate=True)
    assert pre.force_manual
