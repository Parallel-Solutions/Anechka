"""Catalog + data-scope validation for ExportPlan 2.0.

Runs after Pydantic + structural validation. Enforces, independently of the AI:
- every field exists in the Metadata Catalog (whitelist);
- filter operators are valid for the field's data type;
- group_by/sort target groupable/sortable fields;
- sensitive/denied fields respect the role scope;
- data scope: allowed entity types, viewer assigned-only filter, row limits;
- plan complexity limits.
"""

from __future__ import annotations

from typing import Any

from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import Condition, Dataset, ExportPlan2, FieldRef, Sheet
from app.services.export_plan.validator import ExportScope, ValidationResult
from app.services.export_plan.validator_v2 import validate_structure
from app.services.intelligent_export.tomoru_regions import (
    is_filter_placeholder,
    is_tomoru_region_field,
    is_valid_tomoru_region_filter_value,
)

MAX_DATASETS = 10
MAX_SHEETS = 20
MAX_COLUMNS_PER_SHEET = 200
MAX_FILTERS_PER_SCOPE = 40


class CatalogScopeValidator:
    def __init__(self, catalog: FieldCatalog, scope: ExportScope | None = None):
        self.catalog = catalog
        self.scope = scope or ExportScope()

    def validate(self, plan: ExportPlan2) -> ValidationResult:
        result = validate_structure(plan)

        if len(plan.datasets) > MAX_DATASETS:
            result.add("TOO_MANY_DATASETS", f"max {MAX_DATASETS} datasets", "datasets")
        if len(plan.workbook.sheets) > MAX_SHEETS:
            result.add("TOO_MANY_SHEETS", f"max {MAX_SHEETS} sheets", "workbook.sheets")

        for d_idx, dataset in enumerate(plan.datasets):
            self._validate_dataset_scope(dataset, d_idx, result)

        for s_idx, sheet in enumerate(plan.workbook.sheets):
            self._validate_sheet(sheet, s_idx, result)

        return result

    # --- datasets -----------------------------------------------------------
    def _validate_dataset_scope(self, dataset: Dataset, idx: int, result: ValidationResult) -> None:
        path = f"datasets[{idx}]"
        for src in dataset.sources:
            if self.scope.allowed_entity_type_ids and src.entity_type_id not in self.scope.allowed_entity_type_ids:
                result.add(
                    "ENTITY_TYPE_DENIED",
                    f"Entity type {src.entity_type_id} not allowed for role {self.scope.role}",
                    f"{path}.sources",
                )
        if dataset.limit > self.scope.max_rows:
            result.add("ROW_LIMIT_EXCEEDED", f"limit {dataset.limit} exceeds {self.scope.max_rows}", f"{path}.limit")
        if len(dataset.filters) > MAX_FILTERS_PER_SCOPE:
            result.add("TOO_MANY_FILTERS", f"max {MAX_FILTERS_PER_SCOPE} filters", f"{path}.filters")

        for f_idx, filt in enumerate(dataset.filters):
            self._validate_condition(filt, f"{path}.filters[{f_idx}]", result)
        for so_idx, sort in enumerate(dataset.sort):
            self._validate_field(sort.field, f"{path}.sort[{so_idx}].field", result, require_sortable=True)

        # viewer must constrain to own assigned records
        if self.scope.role == "viewer" and self.scope.assigned_by_id is not None:
            has_scope = any(
                f.field.field_code.upper() == "ASSIGNED_BY_ID" and f.op == "eq" and f.value == self.scope.assigned_by_id
                for f in dataset.filters
            )
            if not has_scope:
                result.add(
                    "SCOPE_ASSIGNED_REQUIRED",
                    "Viewer must filter ASSIGNED_BY_ID to own crm user id",
                    f"{path}.filters",
                )

    # --- sheets -------------------------------------------------------------
    def _validate_sheet(self, sheet: Sheet, idx: int, result: ValidationResult) -> None:
        path = f"workbook.sheets[{idx}]"
        if sheet.mode == "parameters":
            return
        if len(sheet.columns) > MAX_COLUMNS_PER_SHEET:
            result.add("TOO_MANY_COLUMNS", f"max {MAX_COLUMNS_PER_SHEET} columns", f"{path}.columns")

        for c_idx, col in enumerate(sheet.columns):
            self._validate_value(col.value, f"{path}.columns[{c_idx}].value", result)
        for f_idx, filt in enumerate(sheet.row_filters):
            self._validate_condition(filt, f"{path}.row_filters[{f_idx}]", result)
        for so_idx, sort in enumerate(sheet.sort):
            self._validate_field(sort.field, f"{path}.sort[{so_idx}].field", result, require_sortable=True)
        for g_idx, gref in enumerate(sheet.group_by):
            self._validate_field(gref, f"{path}.group_by[{g_idx}]", result, require_groupable=True)
        for a_idx, agg in enumerate(sheet.aggregates):
            if agg.field is not None:
                self._validate_field(agg.field, f"{path}.aggregates[{a_idx}].field", result)
        for r_idx, rule in enumerate(sheet.validation_rules):
            if rule.field is not None:
                self._validate_field(rule.field, f"{path}.validation_rules[{r_idx}].field", result)

    # --- helpers ------------------------------------------------------------
    def _validate_condition(self, cond: Condition, path: str, result: ValidationResult) -> None:
        entry = self._validate_field(cond.field, f"{path}.field", result)
        if entry is not None and cond.op not in entry.allowed_filter_ops:
            result.add(
                "FILTER_OP_NOT_ALLOWED",
                f"op {cond.op} not allowed for {entry.field_code} ({entry.data_type})",
                f"{path}.op",
            )
        self._validate_filter_value(cond, path, result)

    def _validate_filter_value(self, cond: Condition, path: str, result: ValidationResult) -> None:
        if cond.op in ("is_null", "is_not_null"):
            return
        values = cond.values if cond.op in ("in", "not_in") else None
        if values is not None:
            for v_idx, value in enumerate(values):
                self._validate_single_filter_value(cond, value, f"{path}.values[{v_idx}]", result)
            return
        self._validate_single_filter_value(cond, cond.value, f"{path}.value", result)

    def _validate_single_filter_value(
        self,
        cond: Condition,
        value: Any,
        path: str,
        result: ValidationResult,
    ) -> None:
        if is_filter_placeholder(value):
            result.add(
                "FILTER_VALUE_PLACEHOLDER",
                f"Filter value looks like an unresolved placeholder: {value!r}",
                path,
            )
            return
        field_code = cond.field.field_code.upper()
        if cond.op == "eq" and is_tomoru_region_field(field_code):
            if not is_valid_tomoru_region_filter_value(value):
                result.add(
                    "FILTER_VALUE_INVALID",
                    f"Region filter requires numeric list element id, got {value!r}",
                    path,
                )

    def _validate_value(self, value: Any, path: str, result: ValidationResult) -> None:
        kind = getattr(value, "kind", None)
        if kind == "field":
            self._validate_field(value.field, f"{path}.field", result)
        elif kind == "aggregate":
            if value.field is not None:
                self._validate_field(value.field, f"{path}.field", result)
        elif kind in ("concat", "coalesce"):
            for p_idx, part in enumerate(value.parts):
                self._validate_value(part, f"{path}.parts[{p_idx}]", result)
        elif kind == "conditional":
            for ci, case in enumerate(value.cases):
                self._validate_condition(case.when, f"{path}.cases[{ci}].when", result)
                self._validate_value(case.then, f"{path}.cases[{ci}].then", result)
            if value.default is not None:
                self._validate_value(value.default, f"{path}.default", result)

    def _validate_field(
        self,
        ref: FieldRef,
        path: str,
        result: ValidationResult,
        *,
        require_sortable: bool = False,
        require_groupable: bool = False,
    ):
        code = ref.field_code.upper()
        entry = self.catalog.get(ref.entity_type_id, code)
        if entry is None or not self.catalog.is_allowed(ref.entity_type_id, code):
            result.add(
                "FIELD_NOT_ALLOWED",
                f"Field {code} not in catalog for entity_type_id={ref.entity_type_id}",
                path,
            )
            return None
        if entry.sensitive and not self.scope.allow_sensitive_fields:
            result.add("FIELD_NOT_ALLOWED", f"Field {code} is sensitive and not allowed for this role", path)
        if require_sortable and not entry.sortable:
            result.add("FIELD_NOT_SORTABLE", f"Field {code} is not sortable", path)
        if require_groupable and not entry.groupable:
            result.add("FIELD_NOT_GROUPABLE", f"Field {code} is not groupable", path)
        return entry
