"""Unit tests for LLM result validator and signal merger."""

from app.services.call_results.deterministic_pre_classifier import PreClassResult
from app.services.call_results.fake_classifier import positive_result
from app.services.call_results.llm_input_builder import LlmInputBuilder
from app.services.call_results.llm_result_validator import LLMResultValidator
from app.services.call_results.signal_merger import SignalMerger


def test_reject_hallucinated_email():
    llm = positive_result(alternate_contact={"name": None, "phone": None, "extension": None, "email": "fake@evil.com", "position": None})
    source = {"transcript": "нужно коммерческое предложение"}
    out = LLMResultValidator().validate(llm, source)
    assert out.result is not None
    assert out.result.alternate_contact.email is None


def test_low_confidence_merge():
    llm = positive_result(confidence=0.45)
    pre = PreClassResult(category=None, reason="llm", llm_required=True)
    merged = SignalMerger().merge(pre, llm, confidence_threshold=0.80, llm_valid=True)
    assert merged.primary_outcome == "manual_review"
    assert merged.requires_manual


def test_llm_input_truncation_flag():
    long_text = "x" * 20000
    bundle = LlmInputBuilder(max_chars=1000).build(
        {"transcript": long_text},
        prompt_version="3",
        schema_version="2",
        model="gpt-4o",
    )
    assert bundle.truncated


def _assert_strict_json_schema(node: dict, *, path: str = "root") -> None:
    if node.get("type") == "object" and "properties" in node:
        props = node["properties"]
        required = set(node.get("required") or [])
        assert required == set(props.keys()), (
            f"{path}: required must list every property key for OpenAI strict mode"
        )
        assert node.get("additionalProperties") is False, f"{path}: additionalProperties must be false"
        for key, sub in props.items():
            if isinstance(sub, dict):
                _assert_strict_json_schema(sub, path=f"{path}.{key}")
    items = node.get("items")
    if isinstance(items, dict):
        _assert_strict_json_schema(items, path=f"{path}[]")


def test_call_result_classification_schema_is_openai_strict():
    from app.services.call_results.llm_schema import CALL_RESULT_CLASSIFICATION_SCHEMA

    _assert_strict_json_schema(CALL_RESULT_CLASSIFICATION_SCHEMA)
    evidence_items = CALL_RESULT_CLASSIFICATION_SCHEMA["properties"]["evidence"]["items"]
    assert evidence_items["required"] == ["source_field", "field", "text"]


def test_llm_result_normalizes_empty_signal_reasons():
    from app.services.call_results.llm_schema import CallResultLLMResult

    result = CallResultLLMResult.model_validate(
        {
            "positive": True,
            "alternate_contact_requested": False,
            "callback_later_requested": False,
            "no_answer": False,
            "explicit_refusal": False,
            "hangup_without_result": False,
            "replacement_contact_required": False,
            "alternate_contact": {
                "name": None,
                "phone": None,
                "extension": None,
                "email": None,
                "position": None,
            },
            "callback_at": None,
            "callback_text": None,
            "summary": "Интерес к КП",
            "refusal_reason": None,
            "confidence": 0.9,
            "needs_manual_review": False,
            "manual_review_reason": None,
            "signal_reasons": {
                "positive": "Запрос КП",
                "alternate_contact_requested": None,
                "callback_later_requested": "",
                "no_answer": None,
                "explicit_refusal": None,
                "hangup_without_result": None,
                "replacement_contact_required": None,
            },
            "evidence": [],
            "primary_outcome": "positive",
        }
    )
    assert result.signal_reasons == {"positive": "Запрос КП"}
