"""LLM gate — decide if row needs LLM classification."""

from __future__ import annotations

from typing import Any

from app.services.call_results.deterministic_pre_classifier import PreClassResult, _has_content


class LlmGate:
    @staticmethod
    def needs_llm(row: dict[str, Any], pre: PreClassResult, *, llm_enabled: bool) -> bool:
        if not llm_enabled:
            return False
        if pre.llm_required:
            return True
        if pre.category is not None and not pre.skip_bitrix:
            return False
        if _has_content(row):
            return True
        technical = (row.get("technical_result") or row.get("category") or "").strip().lower()
        if technical in ("interrupted", "failed", "error", "прерван") and _has_content(row):
            return True
        return False
