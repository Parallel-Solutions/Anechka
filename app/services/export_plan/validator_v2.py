"""ExportPlan 2.0 structural validation.

This layer validates *internal consistency* and *registry references*:
- relation_refs resolve to approved relations with matching entity types;
- column value field refs resolve to a source alias within the sheet dataset;
- transform ops and params are registry-valid;
- validation rule types and params are registry-valid and target real columns.

Catalog (field whitelist) and data-scope checks are added on top in
``catalog_validator`` (Phase D). Pydantic already enforces shape/limits.
"""

from __future__ import annotations

from typing import Any

from app.services.export_plan.models_v2 import (
    Aggregate,
    Column,
    Dataset,
    ExportPlan2,
    FieldRef,
    Sheet,
    ValidationRule,
)
from app.services.export_plan.registry import get_relation, validate_rule_params, validate_transform_params
from app.services.export_plan.validator import ValidationResult


def validate_structure(plan: ExportPlan2) -> ValidationResult:
    result = ValidationResult(valid=True)
    dataset_by_id: dict[str, Dataset] = {d.id: d for d in plan.datasets}

    for d_idx, dataset in enumerate(plan.datasets):
        _validate_dataset(dataset, d_idx, result)

    for s_idx, sheet in enumerate(plan.workbook.sheets):
        _validate_sheet(sheet, s_idx, dataset_by_id, result)

    return result


def _alias_types(dataset: Dataset) -> dict[str, int]:
    return {s.alias: s.entity_type_id for s in dataset.sources}


def _validate_dataset(dataset: Dataset, idx: int, result: ValidationResult) -> None:
    alias_types = _alias_types(dataset)
    for r_idx, rel in enumerate(dataset.relation_refs):
        path = f"datasets[{idx}].relation_refs[{r_idx}]"
        rel_def = get_relation(rel.relation_code)
        if rel_def is None:
            result.add("RELATION_NOT_ALLOWED", f"Unknown relation_code: {rel.relation_code}", path)
            continue
        if rel.from_alias not in alias_types or rel.to_alias not in alias_types:
            result.add("RELATION_ALIAS_UNKNOWN", "relation references unknown alias", path)
            continue
        if alias_types[rel.from_alias] != rel_def.from_entity_type_id:
            result.add("RELATION_TYPE_MISMATCH", "from_alias entity type does not match relation", path)
        if alias_types[rel.to_alias] != rel_def.to_entity_type_id:
            result.add("RELATION_TYPE_MISMATCH", "to_alias entity type does not match relation", path)

    for f_idx, filt in enumerate(dataset.filters):
        _check_field_alias(filt.field, alias_types, f"datasets[{idx}].filters[{f_idx}].field", result)
    for so_idx, sort in enumerate(dataset.sort):
        _check_field_alias(sort.field, alias_types, f"datasets[{idx}].sort[{so_idx}].field", result)


def _validate_sheet(
    sheet: Sheet,
    idx: int,
    dataset_by_id: dict[str, Dataset],
    result: ValidationResult,
) -> None:
    path = f"workbook.sheets[{idx}]"
    if sheet.mode == "parameters":
        return
    dataset = dataset_by_id.get(sheet.dataset_id or "")
    if dataset is None:
        result.add("DATASET_NOT_FOUND", f"sheet references unknown dataset_id: {sheet.dataset_id}", f"{path}.dataset_id")
        return
    alias_types = _alias_types(dataset)
    column_ids = {c.id for c in sheet.columns}

    for c_idx, col in enumerate(sheet.columns):
        _validate_column(col, alias_types, f"{path}.columns[{c_idx}]", result)

    for f_idx, filt in enumerate(sheet.row_filters):
        _check_field_alias(filt.field, alias_types, f"{path}.row_filters[{f_idx}].field", result)
    for s_idx, sort in enumerate(sheet.sort):
        _check_field_alias(sort.field, alias_types, f"{path}.sort[{s_idx}].field", result)
    for g_idx, gref in enumerate(sheet.group_by):
        _check_field_alias(gref, alias_types, f"{path}.group_by[{g_idx}]", result)
    for a_idx, agg in enumerate(sheet.aggregates):
        _validate_aggregate(agg, alias_types, f"{path}.aggregates[{a_idx}]", result)
    for r_idx, rule in enumerate(sheet.validation_rules):
        _validate_rule(rule, alias_types, column_ids, f"{path}.validation_rules[{r_idx}]", result)


def _validate_column(col: Column, alias_types: dict[str, int], path: str, result: ValidationResult) -> None:
    _validate_value(col.value, alias_types, f"{path}.value", result)
    for t_idx, step in enumerate(col.transforms):
        _, err = validate_transform_params(step.op, step.params)
        if err:
            result.add("TRANSFORM_INVALID", err, f"{path}.transforms[{t_idx}]")


def _validate_value(value: Any, alias_types: dict[str, int], path: str, result: ValidationResult) -> None:
    kind = getattr(value, "kind", None)
    if kind == "field":
        _check_field_alias(value.field, alias_types, f"{path}.field", result)
    elif kind == "aggregate":
        if value.func != "count" and value.field is None:
            result.add("AGGREGATE_FIELD_REQUIRED", f"aggregate {value.func} requires a field", path)
        if value.field is not None:
            _check_field_alias(value.field, alias_types, f"{path}.field", result)
    elif kind in ("concat", "coalesce"):
        for p_idx, part in enumerate(value.parts):
            _validate_value(part, alias_types, f"{path}.parts[{p_idx}]", result)
    elif kind == "conditional":
        for ci, case in enumerate(value.cases):
            _check_field_alias(case.when.field, alias_types, f"{path}.cases[{ci}].when.field", result)
            _validate_value(case.then, alias_types, f"{path}.cases[{ci}].then", result)
        if value.default is not None:
            _validate_value(value.default, alias_types, f"{path}.default", result)


def _validate_aggregate(agg: Aggregate, alias_types: dict[str, int], path: str, result: ValidationResult) -> None:
    if agg.func != "count" and agg.field is None:
        result.add("AGGREGATE_FIELD_REQUIRED", f"aggregate {agg.func} requires a field", path)
    if agg.field is not None:
        _check_field_alias(agg.field, alias_types, f"{path}.field", result)


def _validate_rule(
    rule: ValidationRule,
    alias_types: dict[str, int],
    column_ids: set[str],
    path: str,
    result: ValidationResult,
) -> None:
    _, err = validate_rule_params(rule.type, rule.params)
    if err:
        result.add("RULE_INVALID", err, path)
    if rule.column_id is not None and rule.column_id not in column_ids:
        result.add("RULE_COLUMN_UNKNOWN", f"rule references unknown column_id: {rule.column_id}", f"{path}.column_id")
    if rule.field is not None:
        _check_field_alias(rule.field, alias_types, f"{path}.field", result)


def _check_field_alias(ref: FieldRef, alias_types: dict[str, int], path: str, result: ValidationResult) -> None:
    if ref.source_alias is None:
        result.add("SOURCE_ALIAS_MISSING", "field source_alias is required", path)
        return
    if ref.source_alias not in alias_types:
        result.add("SOURCE_ALIAS_UNKNOWN", f"unknown source_alias: {ref.source_alias}", path)
        return
    if alias_types[ref.source_alias] != ref.entity_type_id:
        result.add(
            "FIELD_ENTITY_MISMATCH",
            f"field entity_type_id {ref.entity_type_id} != alias entity type {alias_types[ref.source_alias]}",
            path,
        )
