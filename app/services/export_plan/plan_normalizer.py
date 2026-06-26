"""Normalize common LLM mistakes in ExportPlan JSON before Pydantic validation.

Idempotent: running twice yields the same result. Does not strip unknown keys
so genuine schema errors still surface through the validator.
"""

from __future__ import annotations

import copy
from typing import Any

from app.models import ENTITY_DEAL
from app.services.intelligent_export.tomoru_regions import (
    is_tomoru_region_field,
    try_parse_region_filter_value,
)

_SORT_DIRECTION_SYNONYMS = ("op", "order", "dir", "sort_order", "sort_direction")

_FILTER_OP_SYNONYMS: dict[str, str] = {
    "=": "eq",
    "==": "eq",
    "equals": "eq",
    "equal": "eq",
    "!=": "ne",
    "<>": "ne",
    "not_equals": "ne",
    "not equal": "ne",
    ">": "gt",
    "greater_than": "gt",
    "greater than": "gt",
    ">=": "gte",
    "gte": "gte",
    "greater_or_equal": "gte",
    "greater or equal": "gte",
    "<": "lt",
    "less_than": "lt",
    "less than": "lt",
    "<=": "lte",
    "lte": "lte",
    "less_or_equal": "lte",
    "less or equal": "lte",
    "in": "in",
    "not in": "not_in",
    "not_in": "not_in",
    "contains": "contains",
    "starts_with": "starts_with",
    "starts with": "starts_with",
    "is_null": "is_null",
    "is null": "is_null",
    "null": "is_null",
    "is_not_null": "is_not_null",
    "is not null": "is_not_null",
    "not null": "is_not_null",
    "eq": "eq",
    "ne": "ne",
    "gt": "gt",
}


def normalize_llm_plan(plan_dict: dict) -> dict:
    """Return a deep copy of *plan_dict* with common LLM shape fixes applied."""
    plan = copy.deepcopy(plan_dict)
    _normalize_schema_version(plan)
    for dataset in plan.get("datasets") or []:
        if not isinstance(dataset, dict):
            continue
        for sort_item in dataset.get("sort") or []:
            if isinstance(sort_item, dict):
                _normalize_sort_item(sort_item)
        for filt in dataset.get("filters") or []:
            if isinstance(filt, dict):
                _normalize_condition(filt)
        _sanitize_tomoru_region_filters(dataset)
    workbook = plan.get("workbook")
    if isinstance(workbook, dict):
        for sheet in workbook.get("sheets") or []:
            if not isinstance(sheet, dict):
                continue
            for sort_item in sheet.get("sort") or []:
                if isinstance(sort_item, dict):
                    _normalize_sort_item(sort_item)
            for filt in sheet.get("row_filters") or []:
                if isinstance(filt, dict):
                    _normalize_condition(filt)
            for col in sheet.get("columns") or []:
                if isinstance(col, dict):
                    _normalize_column_value(col.get("value"))
    return plan


def _normalize_schema_version(plan: dict) -> None:
    version = plan.get("schema_version")
    if version is None:
        return
    if version in (2, 2.0, "2", "2.0"):
        plan["schema_version"] = "2.0"


def _normalize_sort_item(item: dict) -> None:
    if "direction" not in item:
        for key in _SORT_DIRECTION_SYNONYMS:
            if key in item:
                item["direction"] = item.pop(key)
                break
    if "direction" in item:
        item["direction"] = _normalize_direction_value(item["direction"])


def _normalize_direction_value(value: Any) -> str:
    if value is None:
        return "asc"
    if isinstance(value, (int, float)):
        if value == -1 or value == -1.0:
            return "desc"
        if value == 1 or value == 1.0:
            return "asc"
    text = str(value).strip().lower()
    if text in ("desc", "descending", "down", "reverse"):
        return "desc"
    if text in ("asc", "ascending", "up"):
        return "asc"
    if text in ("-1",):
        return "desc"
    if text in ("1",):
        return "asc"
    return text


def _normalize_condition(condition: dict) -> None:
    op = condition.get("op")
    if op is None:
        return
    if not isinstance(op, str):
        condition["op"] = _normalize_filter_op(str(op))
    else:
        condition["op"] = _normalize_filter_op(op)
    field = condition.get("field") or {}
    if (field.get("field_code") or "").upper() == "STAGE_ID":
        if condition.get("value") is not None:
            condition["value"] = str(condition["value"])
        if condition.get("values") is not None:
            condition["values"] = [str(v) for v in condition["values"]]


def _normalize_filter_op(op: str) -> str:
    key = op.strip().lower()
    return _FILTER_OP_SYNONYMS.get(key, key)


def _sanitize_tomoru_region_filters(dataset: dict) -> None:
    """Resolve placeholder region ids, drop unresolvable duplicates, dedupe eq filters."""
    filters = dataset.get("filters") or []
    if not filters:
        return

    resolved_by_key: dict[tuple[str, str], int] = {}
    other: list[dict] = []
    region_eq: list[dict] = []

    for filt in filters:
        if not isinstance(filt, dict) or filt.get("op") != "eq":
            other.append(filt)
            continue
        field = filt.get("field") or {}
        if field.get("entity_type_id") != ENTITY_DEAL or not is_tomoru_region_field(field.get("field_code")):
            other.append(filt)
            continue
        alias = field.get("source_alias") or "deal"
        region_id = try_parse_region_filter_value(filt.get("value"))
        key = (alias, field.get("field_code", "").upper())
        if region_id is not None:
            resolved_by_key[key] = region_id
            region_eq.append({**filt, "value": region_id})
        else:
            region_eq.append(filt)

    if not region_eq:
        return

    deduped_region: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for filt in region_eq:
        field = filt.get("field") or {}
        alias = field.get("source_alias") or "deal"
        key = (alias, field.get("field_code", "").upper())
        region_id = resolved_by_key.get(key)
        if region_id is not None:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped_region.append({**filt, "value": region_id})
            continue
        if resolved_by_key:
            continue
        deduped_region.append(filt)

    dataset["filters"] = other + deduped_region


def _normalize_column_value(value: Any) -> None:
    if not isinstance(value, dict):
        return
    kind = value.get("kind")
    if kind == "conditional":
        for case in value.get("cases") or []:
            if isinstance(case, dict):
                when = case.get("when")
                if isinstance(when, dict):
                    _normalize_condition(when)
                then = case.get("then")
                if isinstance(then, dict):
                    _normalize_column_value(then)
        default = value.get("default")
        if isinstance(default, dict):
            _normalize_column_value(default)
    elif kind in ("concat", "coalesce"):
        for part in value.get("parts") or []:
            if isinstance(part, dict):
                _normalize_column_value(part)
