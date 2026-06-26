"""Full intelligent export runner: compile -> stream -> transform/validate -> write.

Runs inside a JobService worker thread. Streams rows in batches (bounded by the
dataset limit and the user's scope max_rows), applies transforms + validation
per sheet with error routing, builds multi-sheet XLSX or single-sheet CSV, and
reports progress / honours cancellation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.exceptions import ExportCancelledError
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.compiler_v2 import ExportPlanCompilerV2
from app.services.export_plan.models_v2 import ExportPlan2, Sheet
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.company_contact_enricher import enrich_company_contacts_for_deals
from app.services.intelligent_export.contact_phone_heuristic import build_tomoru_phone_rows
from app.services.intelligent_export.dictionaries import build_dictionary_tools
from app.services.intelligent_export.output_engine import (
    MultiSheetCsvNotSupported,
    RenderedColumn,
    RenderedSheet,
    write_csv,
    write_xlsx,
)
from app.services.intelligent_export.row_builder import get_field_raw, resolve_row
from app.services.intelligent_export.sheet_processor import StopExport, aggregate_rows, process_sheet
from app.services.intelligent_export.transform_engine import TransformContext
from app.services.lpr_service import load_lpr_config

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000


@dataclass
class RunResult:
    filepath: Path
    sheet_summaries: dict[str, dict] = field(default_factory=dict)
    total_rows: int = 0


class IntelligentExportRunner:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        portal_id: str,
        scope: ExportScope,
        catalog: FieldCatalog,
        *,
        cancel_check: Callable[[], bool] | None = None,
        progress: Callable[[int, int, str], None] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id
        self.scope = scope
        self.catalog = catalog
        self.compiler = ExportPlanCompilerV2(db, portal_id, catalog, scope)
        self.cancel_check = cancel_check or (lambda: False)
        self.progress = progress or (lambda c, t, s: None)
        self.log = log or (lambda m: None)
        resolve_label, dict_check = build_dictionary_tools(db, portal_id)
        self.transform_ctx = TransformContext(resolve_dictionary=resolve_label)
        self.dict_check = dict_check

    def _check_cancel(self) -> None:
        if self.cancel_check():
            raise ExportCancelledError()

    def _stream_dataset(
        self,
        plan: ExportPlan2,
        dataset_id: str,
        compiled_cache: dict,
        *,
        sheet_name: str,
    ) -> list[dict]:
        dataset = next(d for d in plan.datasets if d.id == dataset_id)
        if dataset_id not in compiled_cache:
            compiled_cache[dataset_id] = self.compiler.compile_dataset(dataset)
        compiled = compiled_cache[dataset_id]
        cap = min(dataset.limit, self.scope.max_rows)
        rows: list[dict] = []
        offset = 0
        self.progress(0, cap, f"Лист «{sheet_name}»: загрузка 0/{cap}")
        while offset < cap:
            self._check_cancel()
            limit = min(BATCH_SIZE, cap - offset)
            batch = self.compiler.fetch_page(
                compiled, offset=offset, limit=limit, timeout_ms=self.settings.ie_statement_timeout_ms
            )
            rows.extend(batch)
            fetched = len(rows)
            self.progress(fetched, cap, f"Лист «{sheet_name}»: загрузка {fetched}/{cap}")
            if len(batch) < limit:
                break
            offset += limit
        return rows

    def run(self, plan: ExportPlan2, *, dest_path: Path) -> RunResult:
        compiled_cache: dict = {}
        rendered: list[RenderedSheet] = []
        summaries: dict[str, dict] = {}
        total = len(plan.workbook.sheets)
        total_rows = 0

        is_csv = plan.workbook.format == "csv"
        has_explicit_errors_sheet = any(s.mode == "errors" for s in plan.workbook.sheets)
        routed_errors: list[tuple[Sheet, list[dict]]] = []
        for idx, sheet in enumerate(plan.workbook.sheets, 1):
            self._check_cancel()
            if is_csv and sheet.mode in ("parameters", "errors"):
                # CSV is a single tabular sheet — auxiliary sheets are omitted
                continue
            if sheet.mode == "parameters":
                self.progress(idx, total, f"Лист «{sheet.name}»")
                rendered.append(self._parameters_sheet(plan, sheet))
                continue

            raw_entities = self._stream_dataset(
                plan, sheet.dataset_id, compiled_cache, sheet_name=sheet.name
            )

            if sheet.mode == "aggregate":
                resolved = [self._resolve_aggregate_row(sheet, r) for r in raw_entities]
                rows = aggregate_rows(sheet, resolved)
                rendered.append(self._render(sheet, rows))
                summaries[sheet.id] = {"valid_count": len(rows), "mode": "aggregate"}
                total_rows += len(rows)
                self.progress(idx, total, f"Лист «{sheet.name}» готов")
                continue

            if sheet.post_process is not None and sheet.post_process.op == "tomoru_phones":
                lpr_config = load_lpr_config(self.db)
                if sheet.post_process.fetch_company_contacts_live:
                    enrich_company_contacts_for_deals(
                        self.db,
                        self.portal_id,
                        raw_entities,
                        deal_alias=sheet.post_process.deal_alias,
                        settings=self.settings,
                        log=self.log,
                    )
                out_rows, tomoru_stats = build_tomoru_phone_rows(
                    self.db,
                    self.portal_id,
                    raw_entities,
                    post_process=sheet.post_process,
                    lpr_config=lpr_config,
                    settings=self.settings,
                    log=self.log,
                )
                summary = {
                    "valid_count": len(out_rows),
                    "error_count": 0,
                    "warning_count": 0,
                    "error_rows": 0,
                    "mode": "tomoru_phones",
                    "tomoru_stats": tomoru_stats.__dict__,
                }
                summaries[sheet.id] = summary
                rendered.append(self._render(sheet, out_rows))
                total_rows += len(out_rows)
                self.log(
                    f"Лист «{sheet.name}» (Tomoru): {len(out_rows)} телефонов, "
                    f"пропущено архив={tomoru_stats.deals_skipped_archived}, "
                    f"dedup={tomoru_stats.phones_deduped}"
                )
                self.progress(idx, total, f"Лист «{sheet.name}» готов")
                continue

            resolved_rows = [resolve_row(sheet, r, self.catalog) for r in raw_entities]
            try:
                data_rows, summary, error_rows = process_sheet(
                    sheet,
                    resolved_rows,
                    self.catalog,
                    transform_ctx=self.transform_ctx,
                    dict_check=self.dict_check,
                    raise_on_stop=True,
                )
            except StopExport as exc:
                raise ValueError(f"Лист «{sheet.name}»: {exc.reason}") from exc

            summaries[sheet.id] = summary
            out_rows = error_rows if sheet.mode == "errors" else data_rows
            if sheet.mode != "errors" and error_rows and not has_explicit_errors_sheet:
                routed_errors.append((sheet, error_rows))
            rendered.append(self._render(sheet, out_rows, include_errors=(sheet.mode == "errors")))
            total_rows += len(out_rows)
            self.log(f"Лист «{sheet.name}»: {len(out_rows)} строк, ошибок {summary.get('error_count', 0)}")
            self.progress(idx, total, f"Лист «{sheet.name}» готов")

        if plan.workbook.include_errors_sheet and routed_errors:
            error_total = sum(len(rows) for _, rows in routed_errors)
            if is_csv:
                # CSV is a single tabular sheet — routed error rows cannot be a
                # separate sheet, so record them in the summary for visibility.
                summaries["_dropped_errors"] = {
                    "error_rows": error_total,
                    "note": "CSV не поддерживает лист ошибок — строки с ошибками не включены",
                }
                self.log(f"CSV: {error_total} строк с ошибками не включены в файл")
            else:
                rendered.append(self._auto_errors_sheet(routed_errors))
                # use "rows" (not "error_rows") so the run's error_row_count,
                # summed from the source sheets, is not double-counted.
                summaries["_auto_errors"] = {"rows": error_total, "mode": "errors"}
                total_rows += error_total

        self._check_cancel()
        self.progress(total, total, "Запись файла")
        if plan.workbook.format == "csv":
            try:
                write_csv(rendered, dest_path)
            except MultiSheetCsvNotSupported as exc:
                raise ValueError(str(exc)) from exc
        else:
            write_xlsx(rendered, dest_path)

        return RunResult(filepath=dest_path, sheet_summaries=summaries, total_rows=total_rows)

    # --- rendering ----------------------------------------------------------
    def _render(self, sheet: Sheet, rows: list[dict], *, include_errors: bool = False) -> RenderedSheet:
        columns = [
            RenderedColumn(id=c.id, header=c.header, excel_format=c.excel_format, width=c.width)
            for c in sheet.columns
        ]
        if include_errors:
            columns.append(RenderedColumn(id="_errors", header="Ошибки", width=40))
        return RenderedSheet(name=sheet.name, columns=columns, rows=rows)

    def _auto_errors_sheet(self, routed_errors: list[tuple[Sheet, list[dict]]]) -> RenderedSheet:
        base_sheet = routed_errors[0][0]
        columns = [
            RenderedColumn(id=c.id, header=c.header, excel_format=c.excel_format, width=c.width)
            for c in base_sheet.columns
        ]
        columns.append(RenderedColumn(id="_errors", header="Ошибки", width=40))
        rows: list[dict] = []
        for _sheet, err_rows in routed_errors:
            rows.extend(err_rows)
        return RenderedSheet(name="Ошибки", columns=columns, rows=rows)

    def _resolve_aggregate_row(self, sheet: Sheet, raw_row: dict) -> dict:
        record: dict[str, Any] = {}
        for col in sheet.columns:
            kind = getattr(col.value, "kind", None)
            if kind == "field":
                record[col.id] = get_field_raw(raw_row, col.value.field, self.catalog)
            elif kind == "aggregate" and col.value.field is not None:
                record[col.id] = get_field_raw(raw_row, col.value.field, self.catalog)
            else:
                record[col.id] = None
        return record

    def _parameters_sheet(self, plan: ExportPlan2, sheet: Sheet) -> RenderedSheet:
        columns = [RenderedColumn(id="param", header="Параметр", width=30), RenderedColumn(id="value", header="Значение", width=60)]
        rows = [
            {"param": "Название", "value": plan.title},
            {"param": "Описание", "value": plan.description or ""},
            {"param": "Формат", "value": plan.workbook.format},
            {"param": "Датасеты", "value": ", ".join(d.id for d in plan.datasets)},
        ]
        return RenderedSheet(name=sheet.name, columns=columns, rows=rows)
