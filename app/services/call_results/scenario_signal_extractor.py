"""Deterministic signal extraction from Tomoru scenario_events (LLM fallback)."""

from __future__ import annotations

from typing import Any

from app.services.call_results.contact_number_extractor import extract_contact_number, extract_lpr_from_events
from app.services.call_results.deterministic_pre_classifier import (
    CALLBACK_KEYWORDS,
    HOT_KEYWORDS,
    REFUSAL_KEYWORDS,
)
from app.services.call_results.llm_schema import CallResultSignals, EvidenceItem

ENTRY_FIELD = "вход"
CALLBACK_FIELD = "когда перезвонить"
CONTACT_FIELDS = ("выход на лпр", "номер лпр", "перевод", "вход в диалог")
POSITIVE_FIELDS = ("тз", "когда закупка", "вопрос 1", "вопрос 2")


def _norm_field(field: str | None) -> str:
    return (field or "").strip().lower()


def _event_text(ev: dict[str, Any]) -> str:
    parts = [str(ev.get("match") or ""), str(ev.get("transcription") or "")]
    return " ".join(p for p in parts if p).strip()


def _contains_keyword(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(k in lower for k in keywords)


def _is_refusal_match(match: str, text: str) -> bool:
    m = match.strip().lower()
    if m in ("нет", "no", "не нужно", "не интересно"):
        return True
    return _contains_keyword(text, REFUSAL_KEYWORDS)


def extract_signals_from_scenario_events(row: dict[str, Any]) -> CallResultSignals | None:
    """Extract business signals from Tomoru scenario_events when LLM is unavailable."""
    if not row.get("has_meaningful_content"):
        return None

    events = row.get("scenario_events")
    if not isinstance(events, list) or not events:
        return None

    sig = CallResultSignals()
    reasons: dict[str, str] = {}
    evidence: list[EvidenceItem] = []
    source_text = " ".join(_event_text(ev) for ev in events if isinstance(ev, dict))

    for ev in events:
        if not isinstance(ev, dict):
            continue
        field = _norm_field(ev.get("field"))
        text = _event_text(ev)
        if not text:
            continue

        if field == ENTRY_FIELD or field.startswith(ENTRY_FIELD):
            if _is_refusal_match(str(ev.get("match") or ""), text):
                sig.explicit_refusal = True
                sig.refusal_reason = text[:200]
                reasons["explicit_refusal"] = f"Поле {ev.get('field')}: отказ"
                evidence.append(EvidenceItem(source_field=str(ev.get("source_column") or ev.get("field") or ""), text=text[:300]))

        if CALLBACK_FIELD in field:
            sig.callback_later_requested = True
            sig.callback_text = str(ev.get("match") or text)[:500]
            reasons["callback_later_requested"] = f"Поле {ev.get('field')}"
            evidence.append(EvidenceItem(source_field=str(ev.get("source_column") or ev.get("field") or ""), text=text[:300]))

        if any(cf in field for cf in CONTACT_FIELDS):
            if text.strip():
                sig.alternate_contact_requested = True
                reasons.setdefault("alternate_contact_requested", f"Поле {ev.get('field')}")
                evidence.append(EvidenceItem(source_field=str(ev.get("source_column") or ev.get("field") or ""), text=text[:300]))
                num = extract_contact_number(ev.get("match") or ev.get("transcription"), source_text=source_text)
                ac = sig.alternate_contact
                if num.extension and not ac.extension:
                    ac.extension = num.extension
                if num.full_phone and not ac.phone:
                    ac.phone = num.full_phone
                if not ac.name and ev.get("match"):
                    match_val = str(ev.get("match")).strip()
                    if match_val and not match_val.isdigit() and len(match_val) > 2:
                        ac.name = match_val[:200]

        if any(pf in field for pf in POSITIVE_FIELDS) and _contains_keyword(text, HOT_KEYWORDS):
            sig.positive = True
            reasons["positive"] = f"Поле {ev.get('field')}: ключевые слова"
            evidence.append(EvidenceItem(source_field=str(ev.get("source_column") or ev.get("field") or ""), text=text[:300]))

    lpr = extract_lpr_from_events(events)
    if lpr and (lpr.extension or lpr.full_phone):
        sig.alternate_contact_requested = True
        reasons.setdefault("alternate_contact_requested", "номер ЛПР")
        ac = sig.alternate_contact
        if lpr.extension:
            ac.extension = lpr.extension
        if lpr.full_phone:
            ac.phone = lpr.full_phone
        if lpr.requires_review:
            sig.needs_manual_review = True
            sig.manual_review_reason = "Добавочный/номер требует проверки"

    if _contains_keyword(source_text, CALLBACK_KEYWORDS) and not sig.callback_later_requested:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            text = _event_text(ev)
            if _contains_keyword(text, CALLBACK_KEYWORDS):
                sig.callback_later_requested = True
                sig.callback_text = text[:500]
                reasons["callback_later_requested"] = "Ключевые слова перезвона"
                evidence.append(EvidenceItem(source_field=str(ev.get("source_column") or ev.get("field") or ""), text=text[:300]))
                break

    if sig.active_signal_count() == 0:
        return None

    sig.signal_reasons = reasons
    sig.evidence = evidence[:5]
    sig.confidence = 0.72
    if sig.positive and not sig.evidence:
        sig.needs_manual_review = True
        sig.manual_review_reason = "Положительный результат без evidence"
    elif sig.positive:
        sig.confidence = 0.70
        sig.needs_manual_review = True
        sig.manual_review_reason = sig.manual_review_reason or "Положительный результат по ключевым словам"

    parts = []
    if sig.explicit_refusal:
        parts.append("Отказ")
    if sig.callback_later_requested:
        parts.append("Перезвон")
    if sig.alternate_contact_requested:
        parts.append("Другой контакт")
    if sig.positive:
        parts.append("Положительный")
    sig.summary = ", ".join(parts) if parts else "Сигналы из scenario_events"

    return sig
