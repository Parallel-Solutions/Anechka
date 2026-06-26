"""Synchronous preview: per-sheet counts and a small sample of rows.

Reads only local PostgreSQL via the compiler. Transforms and row validation
are layered on by the engines (Phase F); in their absence preview returns raw
resolved values.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.compiler_v2 import CompiledDataset, ExportPlanCompilerV2
from app.services.export_plan.models_v2 import ExportPlan2, Sheet
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.company_contact_enricher import enrich_company_contacts_for_deals
from app.services.intelligent_export.contact_phone_heuristic import build_tomoru_phone_rows
from app.services.intelligent_export.row_builder import column_headers, resolve_row
from app.services.lpr_service import load_lpr_config

# Optional hook installed by Phase F to apply transforms + validation per sheet.
# Signature: (sheet, rows[list[dict]], catalog) -> (rows, validation_summary, error_rows)
SheetProcessor = Callable[[Sheet, list[dict], FieldCatalog], tuple[list[dict], dict, list[dict]]]

_JSON_SCALAR_TYPES = (str, int, float, bool, type(None))


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, _JSON_SCALAR_TYPES):
        return value
    return str(value)


def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(val) for key, val in row.items()}


class PreviewService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        portal_id: str,
        scope: ExportScope,
        catalog: FieldCatalog,
        *,
        sheet_processor: SheetProcessor | None = None,
    ):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id
        self.scope = scope
        self.catalog = catalog
        self.compiler = ExportPlanCompilerV2(db, portal_id, catalog, scope)
        self.sheet_processor = sheet_processor

    def _compiled_for(self, plan: ExportPlan2, cache: dict[str, CompiledDataset], dataset_id: str) -> CompiledDataset:
        if dataset_id not in cache:
            dataset = next(d for d in plan.datasets if d.id == dataset_id)
            cache[dataset_id] = self.compiler.compile_dataset(dataset)
        return cache[dataset_id]

    def _row_cap(self, dataset) -> int:
        """Rows the export will actually emit for this dataset (limit + scope)."""
        return min(dataset.limit, self.scope.max_rows)

    def _used_dataset_ids(self, plan: ExportPlan2) -> list[str]:
        """Dataset ids referenced by output sheets, in first-seen order."""
        seen: set[str] = set()
        ordered: list[str] = []
        for sheet in plan.workbook.sheets:
            if sheet.mode not in ("rows", "aggregate", "errors") or not sheet.dataset_id:
                continue
            if sheet.dataset_id in seen:
                continue
            seen.add(sheet.dataset_id)
            ordered.append(sheet.dataset_id)
        return ordered

    def count_datasets(self, plan: ExportPlan2) -> dict[str, int]:
        cache: dict[str, CompiledDataset] = {}
        counts: dict[str, int] = {}
        dataset_by_id = {d.id: d for d in plan.datasets}
        for dataset_id in self._used_dataset_ids(plan):
            dataset = dataset_by_id.get(dataset_id)
            if dataset is None:
                continue
            compiled = self._compiled_for(plan, cache, dataset_id)
            raw = self.compiler.count(compiled, timeout_ms=self.settings.ie_statement_timeout_ms)
            counts[dataset_id] = min(raw, self._row_cap(dataset))
        return counts

    def preview(self, plan: ExportPlan2, *, preview_rows: int | None = None) -> dict[str, Any]:
        preview_rows = preview_rows or self.settings.ie_preview_rows
        cache: dict[str, CompiledDataset] = {}
        sheets_out: list[dict[str, Any]] = []
        dataset_counts: dict[str, int] = {}
        warnings: list[str] = []
        dataset_by_id = {d.id: d for d in plan.datasets}
        routed_errors: list[tuple[Sheet, list[dict]]] = []
        has_explicit_errors_sheet = any(s.mode == "errors" for s in plan.workbook.sheets)

        for sheet in plan.workbook.sheets:
            if sheet.mode == "parameters":
                sheets_out.append(self._parameters_sheet(plan, sheet))
                continue
            if sheet.mode == "aggregate":
                sheets_out.append(
                    {
                        "sheet_id": sheet.id,
                        "name": sheet.name,
                        "mode": sheet.mode,
                        "columns": column_headers(sheet.columns),
                        "rows": [],
                        "total_count": dataset_counts.get(sheet.dataset_id, 0),
                        "validation_summary": {},
                        "note": "Сводные листы формируются при полной выгрузке",
                    }
                )
                continue

            dataset = dataset_by_id.get(sheet.dataset_id)
            cap = self._row_cap(dataset) if dataset is not None else preview_rows
            compiled = self._compiled_for(plan, cache, sheet.dataset_id)
            count = min(self.compiler.count(compiled, timeout_ms=self.settings.ie_statement_timeout_ms), cap)
            dataset_counts[sheet.dataset_id] = count
            raw_rows = self.compiler.fetch_page(
                compiled, offset=0, limit=min(preview_rows, cap), timeout_ms=self.settings.ie_statement_timeout_ms
            )
            validation_summary: dict[str, Any] = {}
            error_rows: list[dict] = []

            if sheet.post_process is not None and sheet.post_process.op == "tomoru_phones":
                lpr_config = load_lpr_config(self.db)
                if sheet.post_process.fetch_company_contacts_live:
                    enrich_company_contacts_for_deals(
                        self.db,
                        self.portal_id,
                        raw_rows,
                        deal_alias=sheet.post_process.deal_alias,
                        settings=self.settings,
                    )
                tomoru_rows, tomoru_stats = build_tomoru_phone_rows(
                    self.db,
                    self.portal_id,
                    raw_rows,
                    post_process=sheet.post_process,
                    lpr_config=lpr_config,
                    settings=self.settings,
                )
                rows = [_json_safe_row(r) for r in tomoru_rows]
                validation_summary = {
                    "mode": "tomoru_phones",
                    "tomoru_stats": tomoru_stats.__dict__,
                }
            else:
                rows = [_json_safe_row(resolve_row(sheet, r, self.catalog)) for r in raw_rows]
                if self.sheet_processor is not None:
                    rows, validation_summary, error_rows = self.sheet_processor(sheet, rows, self.catalog)

            if sheet.mode == "errors":
                rows = error_rows
            elif error_rows and not has_explicit_errors_sheet:
                routed_errors.append((sheet, error_rows))

            sheets_out.append(
                {
                    "sheet_id": sheet.id,
                    "name": sheet.name,
                    "mode": sheet.mode,
                    "columns": column_headers(sheet.columns),
                    "rows": rows,
                    "total_count": len(rows) if sheet.mode == "errors" else count,
                    "validation_summary": validation_summary,
                }
            )

        if plan.workbook.include_errors_sheet and routed_errors:
            sheets_out.append(self._auto_errors_sheet(routed_errors))

        total_count = sum(dataset_counts.values())
        return {
            "total_count": total_count,
            "dataset_counts": dataset_counts,
            "sheets": sheets_out,
            "warnings": warnings,
        }

    def _auto_errors_sheet(self, routed_errors: list[tuple[Sheet, list[dict]]]) -> dict[str, Any]:
        base_sheet = routed_errors[0][0]
        columns = column_headers(base_sheet.columns)
        columns.append({"id": "_errors", "header": "Ошибки", "width": 40, "excel_format": None})
        rows: list[dict] = []
        for _sheet, err_rows in routed_errors:
            rows.extend(_json_safe_row(r) for r in err_rows)
        return {
            "sheet_id": "_auto_errors",
            "name": "Ошибки",
            "mode": "errors",
            "columns": columns,
            "rows": rows,
            "total_count": len(rows),
            "validation_summary": {"error_rows": len(rows)},
        }

    def _parameters_sheet(self, plan: ExportPlan2, sheet: Sheet) -> dict[str, Any]:
        params = [
            {"id": "param", "header": "Параметр"},
            {"id": "value", "header": "Значение"},
        ]
        rows = [
            {"param": "Название", "value": plan.title},
            {"param": "Описание", "value": plan.description or ""},
            {"param": "Датасеты", "value": ", ".join(d.id for d in plan.datasets)},
            {"param": "Формат", "value": plan.workbook.format},
        ]
        return {
            "sheet_id": sheet.id,
            "name": sheet.name,
            "mode": sheet.mode,
            "columns": params,
            "rows": rows,
            "total_count": len(rows),
            "validation_summary": {},
        }
