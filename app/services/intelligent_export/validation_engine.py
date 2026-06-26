"""Row validation engine for ExportPlan 2.0 sheets.

Rules run against the *post-transform* column outputs. Each violation is
captured with column/rule/severity. The sheet ``error_policy`` decides what to
do with rows that have errors:
  - stop:           abort the export with an error;
  - valid_only:     drop rows with errors from the data sheet;
  - warn:           keep rows, only report counts;
  - route_to_errors: keep valid rows in the data sheet and copy invalid rows
                     (with reasons) into an errors sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.services.export_plan.models_v2 import Sheet, ValidationRule
from app.services.export_plan.registry import validate_rule_params

DictionaryChecker = Callable[[str, Any], bool]


@dataclass
class RowError:
    row_index: int
    column_id: str | None
    rule_id: str
    rule_type: str
    severity: str
    message: str


@dataclass
class SheetValidationResult:
    valid_rows: list[dict]
    error_rows: list[dict]  # rows + "_errors" reasons
    errors: list[RowError] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    stopped: bool = False
    stop_reason: str | None = None


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _check_rule(rule: ValidationRule, params: Any, value: Any, seen_unique: set, dict_check: DictionaryChecker | None) -> str | None:
    rt = rule.type
    if rt == "required":
        return "обязательное поле пусто" if _is_empty(value) else None
    if rt == "not_empty_after_transform":
        return "значение пусто после преобразований" if _is_empty(value) else None
    if _is_empty(value):
        # other rules ignore empty values (use 'required' to forbid empties)
        return None
    if rt == "string":
        return None
    if rt == "number":
        try:
            float(value)
            return None
        except (TypeError, ValueError):
            return "ожидалось число"
    if rt == "date":
        return None if _looks_like_date(value, params.format) else "ожидалась дата"
    if rt == "min_length":
        return None if len(str(value)) >= params.value else f"длина меньше {params.value}"
    if rt == "max_length":
        return None if len(str(value)) <= params.value else f"длина больше {params.value}"
    if rt == "regex":
        try:
            return None if re.fullmatch(params.pattern, str(value)) else "не соответствует шаблону"
        except re.error:
            return "некорректный шаблон"
    if rt == "in_dictionary":
        if dict_check is None:
            return None
        return None if dict_check(params.dictionary_code, value) else "значения нет в справочнике"
    if rt == "unique":
        key = str(value)
        if key in seen_unique:
            return "повтор значения"
        seen_unique.add(key)
        return None
    return None


def _looks_like_date(value: Any, fmt: str | None) -> bool:
    text = str(value)
    if fmt:
        try:
            datetime.strptime(text, fmt)
            return True
        except ValueError:
            return False
    for f in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            datetime.strptime(text[: len(f) + 6], f)
            return True
        except ValueError:
            continue
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def validate_sheet_rows(
    sheet: Sheet,
    rows: list[dict],
    *,
    dict_check: DictionaryChecker | None = None,
) -> SheetValidationResult:
    result = SheetValidationResult(valid_rows=[], error_rows=[])
    unique_trackers: dict[str, set] = {r.id: set() for r in sheet.validation_rules if r.type == "unique"}

    for idx, row in enumerate(rows):
        row_errors: list[str] = []
        has_error = False
        has_warning = False
        for rule in sheet.validation_rules:
            params, perr = validate_rule_params(rule.type, rule.params)
            if perr:
                continue
            value = row.get(rule.column_id) if rule.column_id else None
            seen = unique_trackers.get(rule.id, set())
            msg = _check_rule(rule, params, value, seen, dict_check)
            if msg is None:
                continue
            reason = f"{rule.column_id or rule.id}: {msg}"
            result.errors.append(
                RowError(idx, rule.column_id, rule.id, rule.type, rule.severity, msg)
            )
            if rule.severity == "warning":
                has_warning = True
                result.warning_count += 1
            else:
                has_error = True
                result.error_count += 1
                row_errors.append(reason)

        if has_error and sheet.error_policy == "stop":
            result.stopped = True
            result.stop_reason = f"Строка {idx + 1}: " + "; ".join(row_errors)
            return result
        if has_error and sheet.error_policy in ("valid_only", "route_to_errors"):
            if sheet.error_policy == "route_to_errors":
                err_row = dict(row)
                err_row["_errors"] = "; ".join(row_errors)
                result.error_rows.append(err_row)
            # excluded from valid rows
            continue
        result.valid_rows.append(row)

    return result
