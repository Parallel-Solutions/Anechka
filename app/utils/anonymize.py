"""PII anonymization for AI metadata analysis."""

from __future__ import annotations

import re
from typing import Any

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.I)
PHONE_RE = re.compile(r"(\+?\d[\d\s\-().]{7,}\d)")
URL_RE = re.compile(r"https?://[^\s]+", re.I)
LONG_NUMBER_RE = re.compile(r"\b\d{10,}\b")
PERSON_RE = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]+(?:\s+[А-ЯЁA-Z][а-яёa-z]+){1,3}\b")


def anonymize_string(value: str, max_len: int = 160) -> str:
    text = value.strip()
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    text = URL_RE.sub("[URL]", text)
    text = LONG_NUMBER_RE.sub("[NUMBER]", text)
    text = PERSON_RE.sub("[PERSON]", text)
    if len(text) > max_len:
        text = text[: max_len - 3] + "..."
    return text


def anonymize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return anonymize_string(value)
    if isinstance(value, list):
        return [anonymize_value(v) for v in value[:20]]
    if isinstance(value, dict):
        return {k: anonymize_value(v) for k, v in list(value.items())[:20]}
    return anonymize_string(str(value))


def string_stats(values: list[str]) -> dict[str, Any]:
    filled = [v for v in values if v]
    lengths = [len(v) for v in filled]
    unique = list(dict.fromkeys(filled))
    examples = [anonymize_string(v) for v in unique[:20]]
    return {
        "filled_count": len(filled),
        "unique_count": len(unique),
        "length_min": min(lengths) if lengths else 0,
        "length_max": max(lengths) if lengths else 0,
        "length_median": sorted(lengths)[len(lengths) // 2] if lengths else 0,
        "examples": examples,
    }


def numeric_stats(values: list[float]) -> dict[str, Any]:
    filled = [v for v in values if v is not None]
    if not filled:
        return {"filled_count": 0, "unique_count": 0}
    sorted_vals = sorted(filled)
    n = len(sorted_vals)
    return {
        "filled_count": n,
        "unique_count": len(set(filled)),
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
        "median": sorted_vals[n // 2],
        "q25": sorted_vals[n // 4] if n >= 4 else sorted_vals[0],
        "q75": sorted_vals[(3 * n) // 4] if n >= 4 else sorted_vals[-1],
    }
