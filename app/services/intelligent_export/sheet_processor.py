"""Combine transforms + row validation + error routing for a single sheet.

Used both by the synchronous preview (PreviewService hook) and the full export
runner, so previewed values match the exported file exactly.
"""

from __future__ import annotations

from typing import Any, Callable

from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import Sheet
from app.services.intelligent_export.transform_engine import TransformContext, apply_transforms
from app.services.intelligent_export.validation_engine import DictionaryChecker, _check_rule
from app.services.export_plan.registry import validate_rule_params


class StopExport(Exception):
    """Raised when a sheet's error_policy == 'stop' and a row fails."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def process_sheet(
    sheet: Sheet,
    raw_rows: list[dict],
    catalog: FieldCatalog,
    *,
    transform_ctx: TransformContext,
    dict_check: DictionaryChecker | None = None,
    raise_on_stop: bool = False,
) -> tuple[list[dict], dict, list[dict]]:
    rules = sheet.validation_rules
    unique_trackers: dict[str, set] = {r.id: set() for r in rules if r.type == "unique"}
    data_rows: list[dict] = []
    error_rows: list[dict] = []
    error_count = 0
    warning_count = 0
    stop_reason: str | None = None

    for idx, row in enumerate(raw_rows):
        transformed: dict[str, Any] = {}
        reasons: list[str] = []
        has_error = False

        for col in sheet.columns:
            tval, terr = apply_transforms(row.get(col.id), col.transforms, transform_ctx)
            transformed[col.id] = tval
            if terr:
                has_error = True
                error_count += 1
                reasons.append(f"{col.id}: {terr}")

        for rule in rules:
            params, perr = validate_rule_params(rule.type, rule.params)
            if perr:
                continue
            value = transformed.get(rule.column_id) if rule.column_id else None
            msg = _check_rule(rule, params, value, unique_trackers.get(rule.id, set()), dict_check)
            if msg is None:
                continue
            if rule.severity == "warning":
                warning_count += 1
            else:
                has_error = True
                error_count += 1
                reasons.append(f"{rule.column_id or rule.id}: {msg}")

        if has_error and sheet.error_policy == "stop":
            stop_reason = f"Строка {idx + 1}: " + "; ".join(reasons)
            if raise_on_stop:
                raise StopExport(stop_reason)
            break
        if has_error and sheet.error_policy in ("valid_only", "route_to_errors"):
            if sheet.error_policy == "route_to_errors":
                err_row = dict(transformed)
                err_row["_errors"] = "; ".join(reasons)
                error_rows.append(err_row)
            continue
        data_rows.append(transformed)

    summary = {
        "valid_count": len(data_rows),
        "error_count": error_count,
        "warning_count": warning_count,
        "error_rows": len(error_rows),
        "error_policy": sheet.error_policy,
        "stop_reason": stop_reason,
    }
    return data_rows, summary, error_rows


def make_sheet_processor(
    transform_ctx: TransformContext,
    dict_check: DictionaryChecker | None = None,
) -> Callable[[Sheet, list[dict], FieldCatalog], tuple[list[dict], dict, list[dict]]]:
    def _proc(sheet: Sheet, rows: list[dict], catalog: FieldCatalog):
        return process_sheet(sheet, rows, catalog, transform_ctx=transform_ctx, dict_check=dict_check)

    return _proc


def aggregate_rows(sheet: Sheet, rows: list[dict]) -> list[dict]:
    """Group resolved rows by group_by columns and compute aggregate columns.

    Operates on already-resolved row dicts (column_id -> value). Field columns
    are treated as group keys; aggregate columns compute over the group.
    """
    group_cols = [c for c in sheet.columns if getattr(c.value, "kind", None) == "field"]
    agg_cols = [c for c in sheet.columns if getattr(c.value, "kind", None) == "aggregate"]
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        key = tuple(row.get(c.id) for c in group_cols)
        groups.setdefault(key, []).append(row)

    out: list[dict] = []
    for key, members in groups.items():
        record: dict[str, Any] = {}
        for c, value in zip(group_cols, key):
            record[c.id] = value
        for c in agg_cols:
            record[c.id] = _aggregate(c, members)
        out.append(record)
    return out


def _aggregate(col, members: list[dict]) -> Any:
    func = col.value.func
    if func == "count":
        return len(members)
    field_id = None
    # aggregate value references a field; find a sibling column id is not direct,
    # so aggregate over the aggregate column's own id values if present
    values = [m.get(col.id) for m in members]
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if not nums:
        return 0
    if func == "sum":
        return round(sum(nums), 4)
    if func == "avg":
        return round(sum(nums) / len(nums), 4)
    if func == "min":
        return min(nums)
    if func == "max":
        return max(nums)
    return len(members)
