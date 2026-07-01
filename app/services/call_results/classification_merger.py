"""Merge deterministic and LLM classification."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.call_results.deterministic_pre_classifier import PreClassResult
from app.services.call_results.llm_schema import CallResultLLMResult


@dataclass
class MergedClassification:
    final_category: str
    classification_source: str
    classification_reason: str
    requires_manual: bool = False
    extracted_data: dict | None = None
    llm_category: str | None = None
    deterministic_category: str | None = None
    merge_conflict_reason: str | None = None


class ClassificationMerger:
    def merge(
        self,
        pre: PreClassResult,
        llm: CallResultLLMResult | None,
        *,
        confidence_threshold: float = 0.80,
        llm_valid: bool = True,
        substantial_truncation: bool = False,
        match_requires_manual: bool = False,
    ) -> MergedClassification:
        det_cat = pre.category
        llm_cat = llm.category if llm else None

        if match_requires_manual:
            return MergedClassification(
                final_category="unknown",
                classification_source="deterministic",
                classification_reason=pre.reason,
                requires_manual=True,
                deterministic_category=det_cat,
                llm_category=llm_cat,
            )

        if pre.skip_bitrix and pre.category:
            return MergedClassification(
                final_category=pre.category,
                classification_source="deterministic",
                classification_reason=pre.reason,
                deterministic_category=det_cat,
                llm_category=llm_cat,
            )

        if llm is None:
            if pre.category:
                return MergedClassification(
                    final_category=pre.category,
                    classification_source="deterministic",
                    classification_reason=pre.reason,
                    requires_manual=pre.force_manual,
                    deterministic_category=det_cat,
                )
            return MergedClassification(
                final_category="unknown",
                classification_source="deterministic",
                classification_reason=pre.reason or "LLM недоступна",
                requires_manual=True,
                deterministic_category=det_cat,
            )

        if not llm_valid or substantial_truncation:
            return MergedClassification(
                final_category="unknown",
                classification_source="llm",
                classification_reason="Ответ LLM не прошёл проверку",
                requires_manual=True,
                deterministic_category=det_cat,
                llm_category=llm_cat,
            )

        if llm.confidence < confidence_threshold:
            return MergedClassification(
                final_category="unknown",
                classification_source="llm",
                classification_reason=f"Низкая уверенность ({llm.confidence:.0%})",
                requires_manual=True,
                extracted_data=self._extract(llm),
                deterministic_category=det_cat,
                llm_category=llm_cat,
            )

        conflict = self._detect_conflict(pre, llm)
        if conflict:
            return MergedClassification(
                final_category="unknown",
                classification_source="hybrid",
                classification_reason=conflict,
                requires_manual=True,
                extracted_data=self._extract(llm),
                deterministic_category=det_cat,
                llm_category=llm_cat,
                merge_conflict_reason=conflict,
            )

        if llm.category == "hot_lead" and not llm.evidence:
            return MergedClassification(
                final_category="unknown",
                classification_source="llm",
                classification_reason="hot_lead без evidence",
                requires_manual=True,
                extracted_data=self._extract(llm),
                deterministic_category=det_cat,
                llm_category=llm_cat,
                merge_conflict_reason="hot_lead без evidence",
            )

        if llm.category == "manager_callback" and not (llm.next_action or "").strip():
            return MergedClassification(
                final_category="unknown",
                classification_source="llm",
                classification_reason="manager_callback без next_action",
                requires_manual=True,
                extracted_data=self._extract(llm),
                deterministic_category=det_cat,
                llm_category=llm_cat,
                merge_conflict_reason="manager_callback без next_action",
            )

        source = "llm" if pre.llm_required or pre.category is None else "hybrid"
        return MergedClassification(
            final_category=llm.category,
            classification_source=source,
            classification_reason=llm.reasoning_summary,
            extracted_data=self._extract(llm),
            deterministic_category=det_cat,
            llm_category=llm_cat,
        )

    @staticmethod
    def _detect_conflict(pre: PreClassResult, llm: CallResultLLMResult) -> str | None:
        det = pre.category
        llm_cat = llm.category

        if det == "refusal" and llm_cat == "robot_callback":
            return "Do Not Call vs robot_callback"
        if det == "refusal" and llm_cat == "hot_lead":
            return "refusal vs hot_lead"
        if det == "robot_callback" and llm_cat in ("hot_lead", "manager_callback") and pre.llm_required:
            return None  # LLM allowed when content exists
        if det == "robot_callback" and llm_cat in ("hot_lead", "manager_callback") and not pre.llm_required:
            return "No Answer vs содержательный LLM результат"
        if llm.do_not_call and llm_cat == "robot_callback":
            return "do_not_call vs robot_callback"
        if det == "refusal" and llm_cat != "refusal":
            return f"deterministic refusal vs LLM {llm_cat}"
        return None

    @staticmethod
    def _extract(llm: CallResultLLMResult) -> dict:
        return {
            "contact_name": llm.contact_name,
            "contact_role": llm.contact_role,
            "phone_extension": llm.phone_extension,
            "full_phone": llm.full_phone,
            "email": llm.email,
            "callback_text": llm.callback_text,
            "need": llm.need,
            "purchase_status": llm.purchase_status,
            "request_for_proposal": llm.request_for_proposal,
            "summary": llm.summary,
            "next_action": llm.next_action,
        }
