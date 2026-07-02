"""Validate LLM classification results (v3 signals)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.call_results.llm_input_builder import LlmInputBuilder
from app.services.call_results.llm_schema import CallResultLLMResult


@dataclass
class ValidationOutcome:
    valid: bool
    result: CallResultLLMResult | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _source_field_known(sf: str, source_cols: set[str]) -> bool:
    if not sf:
        return True
    if sf in source_cols:
        return True
    if sf.startswith("data:"):
        bare = sf.removeprefix("data:")
        return bare in source_cols or sf in source_cols
    return f"data:{sf}" in source_cols


class LLMResultValidator:
    def validate(
        self,
        llm_result: CallResultLLMResult,
        source_row: dict,
        *,
        substantial_truncation: bool = False,
    ) -> ValidationOutcome:
        errors: list[str] = []
        warnings: list[str] = []
        source = LlmInputBuilder.source_text(source_row)
        source_cols = LlmInputBuilder.source_columns(source_row)
        result = llm_result.model_validate(llm_result.model_dump())

        if substantial_truncation:
            errors.append("Существенная потеря контекста при обрезке текста")

        ac = result.alternate_contact
        if ac.email:
            if ac.email.lower() not in source:
                warnings.append("Email не найден в исходном тексте")
                ac.email = None

        if ac.phone:
            digits = re.sub(r"\D", "", ac.phone)
            if digits and len(digits) < 10:
                if not ac.extension:
                    ac.extension = digits
                ac.phone = None
                warnings.append("Короткий номер интерпретирован как добавочный")
                result.needs_manual_review = True
                result.manual_review_reason = (
                    result.manual_review_reason or "Добавочный/номер требует проверки"
                )
            elif digits and len(digits) >= 10 and digits[-10:] not in re.sub(r"\D", "", source):
                errors.append("Телефон не найден в исходном тексте")
                ac.phone = None

        if ac.extension:
            ext = re.sub(r"\D", "", ac.extension)
            if ext and ext not in re.sub(r"\D", "", source):
                warnings.append("Добавочный не найден в тексте")
                ac.extension = None

        if ac.name and ac.name.lower() not in source:
            warnings.append("Имя контакта не подтверждено текстом")
            ac.name = None

        for ev in result.evidence:
            sf = ev.effective_field
            if sf and source_cols and not _source_field_known(sf, source_cols):
                warnings.append(f"Evidence source_field не найден: {sf}")
            if ev.text.lower() not in source and ev.text[:20].lower() not in source:
                warnings.append(f"Evidence не подтверждён: {ev.text[:30]}")

        if result.callback_text and result.callback_text.lower() not in source:
            warnings.append("callback_text не подтверждён исходным текстом")

        if result.positive and not result.evidence:
            errors.append("positive требует evidence")

        if result.explicit_refusal and result.positive:
            errors.append("Конфликт: refusal и positive")

        valid = len(errors) == 0
        return ValidationOutcome(valid=valid, result=result if valid else None, errors=errors, warnings=warnings)
