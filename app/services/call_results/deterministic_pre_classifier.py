"""Deterministic pre-classification before LLM (v2 signals)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.call_results.llm_schema import CallResultSignals

TECHNICAL_UNSUPPORTED = {
    "no answer", "not started", "busy", "voicemail", "noanswer", "notstarted",
    "нет ответа", "не ответил", "занято", "автоответчик",
}
TECHNICAL_AMBIGUOUS = {"interrupted", "failed", "error", "прерван", "ошибка"}
FULLY_COMPLETED = {"fully completed", "fully_completed", "completed"}
RELIABLE_DNC = {"do not call", "dnc", "не звонить", "отказ от звонков"}
REFUSAL_KEYWORDS = ["отказ", "не интерес", "не нужно", "не планиру", "не звоните"]
HOT_KEYWORDS = ["кп", "коммерческ", "потребност", "интерес", "тз", "закупк"]
CALLBACK_KEYWORDS = ["перезвон", "добавочн", "позвонить", "связаться", "уточнить"]


@dataclass
class PreClassResult:
    category: str | None
    reason: str
    skip_bitrix: bool = False
    llm_required: bool = False
    force_manual: bool = False
    unsupported_outcome: bool = False
    det_signals: CallResultSignals | None = None


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _has_scenario_content(row: dict[str, Any]) -> bool:
    if row.get("has_meaningful_content"):
        return True
    events = row.get("scenario_events")
    if isinstance(events, list) and events:
        return True
    content = row.get("content_text")
    if content and str(content).strip():
        return True
    return False


def _has_content(row: dict[str, Any]) -> bool:
    if _has_scenario_content(row):
        return True
    for key in ("transcript", "comment", "call_summary"):
        val = row.get(key)
        if val and str(val).strip():
            return True
    scenario = row.get("scenario_answers")
    if isinstance(scenario, dict):
        return any(v for v in scenario.values() if v not in (None, ""))
    return False


def _technical_no_answer_signals(status: str) -> CallResultSignals:
    label = (status or "No Answer").strip()
    return CallResultSignals(
        no_answer=True,
        summary=f"Не дозвонились: {label}",
        confidence=1.0,
        signal_reasons={"no_answer": label},
    )


def _hangup_without_answers_signals() -> CallResultSignals:
    return CallResultSignals(
        hangup_without_result=True,
        replacement_contact_required=True,
        summary="Бросили трубку без ответов",
        confidence=0.95,
        signal_reasons={
            "hangup_without_result": "Interrupted без содержания",
            "replacement_contact_required": "Перезвон на другой номер",
        },
    )


def _is_technical_unsupported(technical: str, call_result: str) -> bool:
    return (
        not technical
        or technical in TECHNICAL_UNSUPPORTED
        or call_result in TECHNICAL_UNSUPPORTED
    )


class DeterministicPreClassifier:
    def classify(self, row: dict[str, Any], *, is_duplicate: bool = False, invalid_phone: bool = False) -> PreClassResult:
        if invalid_phone:
            multi = row.get("phone_multi_status")
            if multi == "multiple":
                return PreClassResult(category=None, reason="Несколько телефонов в строке", force_manual=True)
            return PreClassResult(category=None, reason="Некорректный телефон", force_manual=True)

        if is_duplicate:
            return PreClassResult(
                category="unknown",
                reason="Точный дубликат попытки",
                force_manual=True,
            )

        technical = _norm(row.get("technical_result") or row.get("call_result") or row.get("category"))
        call_result = _norm(row.get("call_result") or row.get("technical_result") or row.get("category"))
        comment = _norm(row.get("comment"))
        has_content = _has_content(row)

        if call_result in RELIABLE_DNC or technical in RELIABLE_DNC:
            sig = CallResultSignals(
                explicit_refusal=True,
                summary="Do Not Call",
                refusal_reason="Do Not Call",
                confidence=1.0,
                signal_reasons={"explicit_refusal": "Do Not Call"},
            )
            return PreClassResult(
                category="refusal",
                reason="Do Not Call",
                det_signals=sig,
            )

        if has_content:
            if call_result in TECHNICAL_AMBIGUOUS or technical in TECHNICAL_AMBIGUOUS:
                return PreClassResult(
                    category=None,
                    reason="Interrupted с содержательными данными — LLM",
                    llm_required=True,
                )
            if call_result in FULLY_COMPLETED:
                return PreClassResult(
                    category=None,
                    reason="Fully Completed — требуется анализ содержания",
                    llm_required=True,
                )
            return PreClassResult(
                category=None,
                reason="Содержательный текст — требуется LLM",
                llm_required=True,
            )

        if not has_content and not comment:
            if _is_technical_unsupported(technical, call_result):
                status_label = call_result or technical or "No Answer"
                return PreClassResult(
                    category="manager_callback",
                    reason=f"Технический статус без содержания: {status_label}",
                    det_signals=_technical_no_answer_signals(status_label),
                )

        if _is_technical_unsupported(technical, call_result) and not has_content:
            status_label = call_result or technical or "No Answer"
            return PreClassResult(
                category="manager_callback",
                reason=f"Технический статус: {status_label}",
                det_signals=_technical_no_answer_signals(status_label),
            )

        if call_result in FULLY_COMPLETED and not has_content:
            return PreClassResult(
                category="unknown",
                reason="Fully Completed без содержания",
                force_manual=True,
            )

        text = " ".join(
            str(row.get(k, ""))
            for k in ("comment", "transcript", "category", "technical_result", "content_text")
        ).lower()

        if any(k in text for k in REFUSAL_KEYWORDS):
            if has_content:
                return PreClassResult(category=None, reason="Возможный отказ — LLM", llm_required=True)
            sig = CallResultSignals(
                explicit_refusal=True,
                summary="Отказ по ключевым словам",
                confidence=0.7,
                signal_reasons={"explicit_refusal": "Ключевые слова отказа"},
            )
            return PreClassResult(category="refusal", reason="Отказ по ключевым словам", det_signals=sig)

        if any(k in text for k in HOT_KEYWORDS) and has_content:
            return PreClassResult(category=None, reason="Возможный лид — LLM", llm_required=True)

        if any(k in text for k in CALLBACK_KEYWORDS):
            if has_content:
                return PreClassResult(category=None, reason="Перезвон — LLM", llm_required=True)
            sig = CallResultSignals(
                callback_later_requested=True,
                callback_text=comment or None,
                confidence=0.6,
                signal_reasons={"callback_later_requested": "Ключевые слова перезвона"},
            )
            return PreClassResult(category="manager_callback", reason="Перезвон по ключевым словам", det_signals=sig)

        if has_content:
            return PreClassResult(category=None, reason="Содержательный текст", llm_required=True)

        if call_result in TECHNICAL_AMBIGUOUS or technical in TECHNICAL_AMBIGUOUS:
            return PreClassResult(
                category="robot_callback",
                reason="Interrupted без содержания — бросили трубку",
                det_signals=_hangup_without_answers_signals(),
            )

        return PreClassResult(category="unknown", reason="Недосточно данных", force_manual=True)
