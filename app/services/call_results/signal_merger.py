"""Merge deterministic hints and LLM signals into final business_signals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.call_results.deterministic_pre_classifier import PreClassResult
from app.services.call_results.scenario_signal_extractor import extract_signals_from_scenario_events
from app.services.call_results.llm_schema import (
    CallResultLLMResult,
    CallResultSignals,
    compute_primary_outcome,
    legacy_category_from_signals,
)


@dataclass
class MergedSignals:
    signals: CallResultSignals
    primary_outcome: str
    final_category: str
    classification_source: str
    classification_reason: str
    requires_manual: bool = False
    extracted_data: dict | None = None
    llm_category: str | None = None
    deterministic_category: str | None = None
    merge_conflict_reason: str | None = None
    skip_bitrix: bool = False
    unsupported_outcome: bool = False


class SignalMerger:
    def merge(
        self,
        pre: PreClassResult,
        llm: CallResultLLMResult | None,
        *,
        confidence_threshold: float = 0.80,
        llm_valid: bool = True,
        substantial_truncation: bool = False,
        match_requires_manual: bool = False,
        match_status: str | None = None,
        match_reason: str | None = None,
        manual_signals: CallResultSignals | None = None,
        normalized_data: dict | None = None,
    ) -> MergedSignals:
        if manual_signals is not None:
            po = compute_primary_outcome(manual_signals)
            return MergedSignals(
                signals=manual_signals,
                primary_outcome=po,
                final_category=legacy_category_from_signals(manual_signals),
                classification_source="manual",
                classification_reason="Ручное исправление",
                requires_manual=manual_signals.needs_manual_review,
                extracted_data=self._extract_from_signals(manual_signals),
            )

        if match_requires_manual:
            match_reason_text = match_reason or pre.reason or "Проблема сопоставления со сделкой"
            if llm is not None and llm_valid:
                sig = llm.to_signals()
                self._apply_match_failure_marker(sig, match_status, match_reason)
                source = "hybrid" if pre.llm_required or pre.category is None else "llm"
                return self._merged(
                    sig,
                    pre,
                    llm,
                    source=source,
                    reason=match_reason_text,
                    manual=sig.needs_manual_review,
                )
            if pre.det_signals and pre.det_signals.active_signal_count() > 0:
                sig = pre.det_signals.model_copy(deep=True)
                self._apply_match_failure_marker(sig, match_status, match_reason)
                return self._merged(sig, pre, llm, source="deterministic", reason=pre.reason)
            fallback = self._scenario_fallback(normalized_data)
            if fallback is not None:
                self._apply_match_failure_marker(fallback, match_status, match_reason)
                return self._merged(
                    fallback,
                    pre,
                    llm,
                    source="deterministic",
                    reason=fallback.summary or match_reason_text,
                )
            sig = self._empty_manual(match_reason_text)
            return self._merged(sig, pre, llm, source="deterministic", reason=pre.reason, manual=True)

        if pre.unsupported_outcome:
            sig = CallResultSignals(
                needs_manual_review=True,
                manual_review_reason=pre.reason,
            )
            return MergedSignals(
                signals=sig,
                primary_outcome="unsupported_outcome",
                final_category="unknown",
                classification_source="deterministic",
                classification_reason=pre.reason,
                requires_manual=True,
                unsupported_outcome=True,
                skip_bitrix=True,
                deterministic_category=pre.category,
            )

        if pre.force_manual:
            sig = self._empty_manual(pre.reason)
            return self._merged(sig, pre, llm, source="deterministic", reason=pre.reason, manual=True)

        if llm is None:
            if pre.det_signals:
                sig = pre.det_signals
                return self._merged(sig, pre, None, source="deterministic", reason=pre.reason)
            fallback = self._scenario_fallback(normalized_data)
            if fallback is not None:
                return self._merged(fallback, pre, None, source="deterministic", reason=fallback.summary or pre.reason)
            sig = self._empty_manual(pre.reason or "LLM недоступна")
            return self._merged(sig, pre, None, source="deterministic", reason=pre.reason, manual=True)

        if not llm_valid or substantial_truncation:
            sig = llm.to_signals()
            if sig.active_signal_count() == 0:
                fallback = self._scenario_fallback(normalized_data)
                if fallback is not None:
                    fallback.needs_manual_review = True
                    fallback.manual_review_reason = fallback.manual_review_reason or "Ответ LLM не прошёл проверку"
                    return self._merged(fallback, pre, llm, source="deterministic", reason=fallback.manual_review_reason, manual=True)
            sig.needs_manual_review = True
            sig.manual_review_reason = sig.manual_review_reason or "Ответ LLM не прошёл проверку"
            return self._merged(sig, pre, llm, source="llm", reason=sig.manual_review_reason, manual=True)

        sig = llm.to_signals()

        if sig.confidence < confidence_threshold:
            sig.needs_manual_review = True
            sig.manual_review_reason = f"Низкая уверенность ({sig.confidence:.0%})"
            return self._merged(sig, pre, llm, source="llm", reason=sig.manual_review_reason, manual=True)

        conflict = self._detect_conflicts(pre, sig)
        if conflict:
            sig.needs_manual_review = True
            sig.manual_review_reason = conflict
            return MergedSignals(
                signals=sig,
                primary_outcome="manual_review",
                final_category="unknown",
                classification_source="hybrid",
                classification_reason=conflict,
                requires_manual=True,
                extracted_data=self._extract_from_signals(sig),
                llm_category=legacy_category_from_signals(sig),
                deterministic_category=pre.category,
                merge_conflict_reason=conflict,
            )

        if sig.positive and not sig.evidence:
            sig.needs_manual_review = True
            sig.manual_review_reason = "Положительный результат без evidence"
            return self._merged(sig, pre, llm, source="llm", reason=sig.manual_review_reason, manual=True)

        source = "llm" if pre.llm_required or pre.category is None else "hybrid"
        return self._merged(sig, pre, llm, source=source, reason=llm.summary or pre.reason)

    def _merged(
        self,
        sig: CallResultSignals,
        pre: PreClassResult,
        llm: CallResultLLMResult | None,
        *,
        source: str,
        reason: str | None,
        manual: bool = False,
    ) -> MergedSignals:
        po = compute_primary_outcome(sig)
        return MergedSignals(
            signals=sig,
            primary_outcome=po,
            final_category=legacy_category_from_signals(sig),
            classification_source=source,
            classification_reason=reason or "",
            requires_manual=manual or sig.needs_manual_review,
            extracted_data=self._extract_from_signals(sig),
            llm_category=legacy_category_from_signals(sig) if llm else None,
            deterministic_category=pre.category,
            skip_bitrix=pre.skip_bitrix,
        )

    @staticmethod
    def _empty_manual(reason: str | None) -> CallResultSignals:
        return CallResultSignals(needs_manual_review=True, manual_review_reason=reason)

    @staticmethod
    def _scenario_fallback(normalized_data: dict | None) -> CallResultSignals | None:
        if not normalized_data:
            return None
        return extract_signals_from_scenario_events(normalized_data)

    @staticmethod
    def _apply_match_failure_marker(
        sig: CallResultSignals,
        match_status: str | None,
        match_reason: str | None,
    ) -> None:
        reason = match_reason or "Проблема сопоставления со сделкой"
        reasons = dict(sig.signal_reasons)
        if match_status == "not_found":
            sig.deal_not_found = True
            reasons["deal_not_found"] = reason
        else:
            reasons["deal_match"] = reason
        sig.signal_reasons = reasons

    @staticmethod
    def _detect_conflicts(pre: PreClassResult, sig: CallResultSignals) -> str | None:
        if sig.explicit_refusal and sig.positive:
            return "Одновременно отказ и положительный результат"
        if sig.explicit_refusal and sig.callback_later_requested:
            return "Одновременно отказ и просьба перезвонить"
        if sig.hangup_without_result and sig.active_signal_count() > 1:
            return "Hangup вместе с другими сигналами"
        ac = sig.alternate_contact
        if sig.alternate_contact_requested and ac.phone and len(str(ac.phone)) < 10:
            return "Неполный номер альтернативного контакта"
        if pre.category == "refusal" and sig.positive:
            return "deterministic refusal vs LLM positive"
        return None

    @staticmethod
    def _extract_from_signals(sig: CallResultSignals) -> dict[str, Any]:
        ac = sig.alternate_contact
        return {
            "contact_name": ac.name,
            "contact_role": ac.position,
            "phone_extension": ac.extension,
            "full_phone": ac.phone,
            "email": ac.email,
            "callback_text": sig.callback_text,
            "summary": sig.summary,
            "refusal_reason": sig.refusal_reason,
            "next_action": sig.summary,
        }

    @staticmethod
    def signals_from_dict(data: dict | None) -> CallResultSignals:
        if not data:
            from app.models.call_results import empty_business_signals
            return CallResultSignals.model_validate(empty_business_signals())
        return CallResultSignals.model_validate(data)
