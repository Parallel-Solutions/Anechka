"""Registry-driven transformation engine (per-cell, per-row error capture).

Each transform op is validated against its typed param model (registry) and
applied deterministically. Errors are captured per row/column instead of
raising, so the row-validation/error-routing layer can decide what to do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from app.services.export_plan.models_v2 import TransformStep
from app.services.export_plan.registry import validate_transform_params
from app.services.phone_service import format_display_phone, normalize_phone

DictionaryResolver = Callable[[str | None, Any], Any]


@dataclass
class TransformContext:
    resolve_dictionary: DictionaryResolver | None = None
    timezone: str = "Europe/Moscow"


def _to_text(value: Any) -> str:
    return "" if value is None else str(value)


def _apply_one(op: str, value: Any, params: Any, ctx: TransformContext) -> tuple[Any, str | None]:
    if op == "trim":
        return (None if value is None else _to_text(value).strip()), None
    if op == "uppercase":
        return (None if value is None else _to_text(value).upper()), None
    if op == "lowercase":
        return (None if value is None else _to_text(value).lower()), None
    if op == "null_to_empty":
        return ("" if value is None else value), None
    if op == "constant":
        return params.value, None
    if op == "default_value":
        return (params.value if (value is None or value == "") else value), None
    if op == "phone_digits_only":
        if value is None or _to_text(value).strip() == "":
            return None, None
        norm = normalize_phone(_to_text(value))
        if norm is None:
            return None, "phone_invalid"
        return norm, None
    if op == "phone_normalize":
        if value is None or _to_text(value).strip() == "":
            return None, None
        norm = normalize_phone(_to_text(value))
        if norm is None:
            return None, "phone_invalid"
        return format_display_phone(norm), None
    if op == "number_round":
        if value is None or value == "":
            return value, None
        try:
            return round(float(value), params.digits), None
        except (TypeError, ValueError):
            return value, "number_invalid"
    if op == "date_format":
        return _date_format(value, params), None
    if op == "dictionary_label":
        if ctx.resolve_dictionary is None:
            return value, None
        label = ctx.resolve_dictionary(params.dictionary_code, value)
        return (label if label is not None else value), None
    if op == "mapping_lookup":
        return _mapping_lookup(value, params)
    return value, f"unknown_op:{op}"


def _date_format(value: Any, params: Any) -> Any:
    if value in (None, ""):
        return value
    text = str(value)
    dt: datetime | None = None
    if params.source_format:
        try:
            dt = datetime.strptime(text, params.source_format)
        except ValueError:
            dt = None
    if dt is None:
        cleaned = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
    if dt is None:
        return value
    return dt.strftime(params.format)


def _mapping_lookup(value: Any, params: Any) -> tuple[Any, str | None]:
    key = _to_text(value)
    if key in params.mapping:
        return params.mapping[key], None
    policy = params.on_unknown
    if policy == "keep_original":
        return value, None
    if policy == "default":
        return params.default, None
    if policy == "warning":
        return value, "mapping_unknown_value"
    if policy == "error":
        return value, "mapping_unknown_value"
    return value, None


def apply_transforms(value: Any, steps: list[TransformStep], ctx: TransformContext) -> tuple[Any, str | None]:
    current = value
    for step in steps:
        params, err = validate_transform_params(step.op, step.params)
        if err:
            return current, err
        current, op_err = _apply_one(step.op, current, params, ctx)
        if op_err:
            return current, op_err
    return current, None
