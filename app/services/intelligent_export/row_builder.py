"""Resolve ExportPlan 2.0 column values from fetched entity rows.

Produces raw (pre-transform) python values for each column. Transforms and row
validation are applied on top by the engines (Phase F). A "row" is a mapping
``{source_alias: CrmEntity | None}`` from the compiler.
"""

from __future__ import annotations

from typing import Any

from app.services.bitrix_import.contact_parser import choose_primary_phone, parse_phones
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import Column, Condition, FieldRef, Sheet
from app.services.export_plan.payload_keys import payload_lookup


def _multifield_primary(value: Any) -> Any:
    """Return primary phone/email from a Bitrix multifield array (MOBILE > WORK > first)."""
    if not isinstance(value, list) or not value:
        return value
    phones = parse_phones(value)
    if phones:
        primary = choose_primary_phone(phones)
        return primary["value"] if primary else None
    first = value[0]
    if isinstance(first, dict):
        return first.get("VALUE") or first.get("value") or first
    return first


def get_field_raw(row: dict[str, Any], ref: FieldRef, catalog: FieldCatalog) -> Any:
    entity = row.get(ref.source_alias)
    if entity is None:
        return None
    code = ref.field_code.upper()
    entry = catalog.get(ref.entity_type_id, code)
    if entry is not None and entry.storage == "column" and entry.column_name:
        return getattr(entity, entry.column_name, None)
    if code == "ID":
        return getattr(entity, "entity_id", None)
    raw = getattr(entity, "raw_payload", None) or {}
    val = payload_lookup(raw, code)
    if entry is not None and entry.is_multiple:
        return _multifield_primary(val)
    return val


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def eval_condition(cond: Condition, row: dict[str, Any], catalog: FieldCatalog) -> bool:
    actual = get_field_raw(row, cond.field, catalog)
    op = cond.op
    if op == "is_null":
        return _is_empty(actual)
    if op == "is_not_null":
        return not _is_empty(actual)
    if op in ("in", "not_in"):
        values = cond.values or []
        present = actual in values
        return present if op == "in" else not present
    target = cond.value
    if op == "eq":
        return str(actual) == str(target)
    if op == "ne":
        return str(actual) != str(target)
    try:
        a = float(actual)
        b = float(target)
        if op == "gt":
            return a > b
        if op == "gte":
            return a >= b
        if op == "lt":
            return a < b
        if op == "lte":
            return a <= b
    except (TypeError, ValueError):
        return False
    if op == "contains":
        return str(target) in str(actual or "")
    if op == "starts_with":
        return str(actual or "").startswith(str(target))
    return False


def resolve_value(value: Any, row: dict[str, Any], catalog: FieldCatalog) -> Any:
    kind = getattr(value, "kind", None)
    if kind == "field":
        return get_field_raw(row, value.field, catalog)
    if kind == "constant":
        return value.value
    if kind == "concat":
        parts = [resolve_value(p, row, catalog) for p in value.parts]
        return value.separator.join(str(p) for p in parts if not _is_empty(p))
    if kind == "coalesce":
        for p in value.parts:
            resolved = resolve_value(p, row, catalog)
            if not _is_empty(resolved):
                return resolved
        return None
    if kind == "conditional":
        for case in value.cases:
            if eval_condition(case.when, row, catalog):
                return resolve_value(case.then, row, catalog)
        if value.default is not None:
            return resolve_value(value.default, row, catalog)
        return None
    if kind == "aggregate":
        # aggregates are computed at sheet level, not per row
        return None
    return None


def resolve_row(sheet: Sheet, row: dict[str, Any], catalog: FieldCatalog) -> dict[str, Any]:
    return {col.id: resolve_value(col.value, row, catalog) for col in sheet.columns}


def column_headers(columns: list[Column]) -> list[dict[str, Any]]:
    return [{"id": c.id, "header": c.header, "width": c.width, "excel_format": c.excel_format} for c in columns]
