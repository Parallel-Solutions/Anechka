"""Bitrix raw_payload key resolution (UPPER_SNAKE vs camelCase)."""

from __future__ import annotations

from typing import Any


def camel_key(code: str) -> str:
    """UPPER_SNAKE Bitrix field code -> camelCase raw_payload key.

    Example: ``LAST_NAME`` -> ``lastName``, ``PHONE`` -> ``phone``.
    """
    parts = code.lower().split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def payload_lookup(raw: dict[str, Any], code: str) -> Any:
    """Look up a field in raw_payload trying UPPER, lower, and camelCase keys."""
    for key in (code, code.lower(), camel_key(code)):
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return None
