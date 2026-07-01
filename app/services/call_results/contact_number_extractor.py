"""Extract contact numbers from Tomoru LPR field."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.phone_service import normalize_phone


@dataclass
class ContactNumberResult:
    full_phone: str | None
    extension: str | None
    raw_value: str
    confidence: float
    requires_review: bool


def extract_contact_number(raw_value: str | None, *, source_text: str | None = None) -> ContactNumberResult:
    text = str(raw_value or "").strip()
    if not text:
        return ContactNumberResult(None, None, text, 0.0, False)

    digits_sequences = re.findall(r"\d+", text)
    all_digits = re.sub(r"\D", "", text)

    full_phone: str | None = None
    extension: str | None = None
    requires_review = False
    confidence = 0.5

    normalized = normalize_phone(text)
    if normalized and len(re.sub(r"\D", "", normalized)) in (10, 11):
        full_phone = normalized
        confidence = 0.9
        if source_text and normalized[-10:] not in re.sub(r"\D", "", source_text or ""):
            requires_review = True
            confidence = 0.6

    if full_phone is None and len(digits_sequences) == 1:
        seq = digits_sequences[0]
        if len(seq) in (10, 11):
            full_phone = normalize_phone(seq)
            confidence = 0.85
        elif 3 <= len(seq) <= 6:
            extension = seq
            confidence = 0.72
            requires_review = True
        elif 7 <= len(seq) <= 9:
            requires_review = True
            confidence = 0.4
    elif len(digits_sequences) > 1:
        phones = [normalize_phone(s) for s in digits_sequences if normalize_phone(s)]
        if len(phones) == 1:
            full_phone = phones[0]
            confidence = 0.7
            requires_review = True
        else:
            requires_review = True
            confidence = 0.3
    elif 3 <= len(all_digits) <= 6 and not full_phone:
        extension = all_digits
        confidence = 0.72
        requires_review = True

    return ContactNumberResult(
        full_phone=full_phone,
        extension=extension,
        raw_value=text,
        confidence=confidence,
        requires_review=requires_review,
    )


def extract_lpr_from_events(scenario_events: list[dict]) -> ContactNumberResult | None:
    for ev in scenario_events:
        field = (ev.get("field") or "").lower()
        if "лпр" in field or "lpr" in field:
            match = ev.get("match") or ""
            transcription = ev.get("transcription") or ""
            source = f"{match} {transcription}"
            return extract_contact_number(match or transcription, source_text=source)
    return None
