"""Fake LLM classifier for tests and dev."""

from __future__ import annotations

from typing import Any, Callable

from app.services.call_results.llm_gateway import BaseCallResultClassifier, ClassifyOutcome
from app.services.call_results.llm_schema import CallResultLLMResult, EvidenceItem


class FakeCallResultClassifier(BaseCallResultClassifier):
    """Deterministic classifier for tests. Pass responses list or callable."""

    def __init__(
        self,
        responses: list[CallResultLLMResult | dict | Exception] | None = None,
        fn: Callable[[dict[str, Any]], ClassifyOutcome] | None = None,
    ):
        self._responses = list(responses or [])
        self._fn = fn
        self._idx = 0
        self.call_count = 0

    def classify(self, input_data: dict[str, Any]) -> ClassifyOutcome:
        self.call_count += 1
        if self._fn:
            return self._fn(input_data)
        if not self._responses:
            return ClassifyOutcome(
                result=CallResultLLMResult(
                    summary="Mock",
                    confidence=0.5,
                ),
                provider="mock",
                model="fake",
            )
        item = self._responses[min(self._idx, len(self._responses) - 1)]
        self._idx += 1
        if isinstance(item, Exception):
            raise item
        if item is None:
            return ClassifyOutcome(result=None, error_type="mock_skip", provider="mock", model="fake")
        if isinstance(item, dict):
            item = CallResultLLMResult.model_validate(item)
        return ClassifyOutcome(result=item, provider="mock", model="fake", duration_ms=1)


def positive_result(**overrides) -> CallResultLLMResult:
    base = dict(
        positive=True,
        summary="Клиент заинтересован, нужно КП",
        confidence=0.92,
        signal_reasons={"positive": "Есть потребность и запрос КП"},
        evidence=[EvidenceItem(field="transcript", text="нужно коммерческое предложение")],
    )
    base.update(overrides)
    return CallResultLLMResult.model_validate(base)


def alternate_contact_result(**overrides) -> CallResultLLMResult:
    base = dict(
        alternate_contact_requested=True,
        alternate_contact={"name": "Иван", "phone": "+79001234567", "extension": None, "email": None, "position": None},
        summary="Дали другой номер",
        confidence=0.88,
        signal_reasons={"alternate_contact_requested": "Новый контакт"},
        evidence=[EvidenceItem(field="transcript", text="позвоните Ивану")],
    )
    base.update(overrides)
    return CallResultLLMResult.model_validate(base)


def callback_later_result(**overrides) -> CallResultLLMResult:
    base = dict(
        callback_later_requested=True,
        callback_text="завтра в 15:00",
        summary="Просьба перезвонить позже",
        confidence=0.88,
        signal_reasons={"callback_later_requested": "Запрос перезвона"},
        evidence=[EvidenceItem(field="transcript", text="завтра в 15:00")],
    )
    base.update(overrides)
    return CallResultLLMResult.model_validate(base)


def refusal_result(**overrides) -> CallResultLLMResult:
    base = dict(
        explicit_refusal=True,
        summary="Не интересно",
        refusal_reason="Нет потребности",
        confidence=0.9,
        signal_reasons={"explicit_refusal": "Явный отказ"},
        evidence=[EvidenceItem(field="transcript", text="не интересно")],
    )
    base.update(overrides)
    return CallResultLLMResult.model_validate(base)


# backward compat aliases
hot_lead_result = positive_result
manager_callback_result = alternate_contact_result
