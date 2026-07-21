"""Export call result plan to JSON/XLSX/CSV."""

from __future__ import annotations

import csv
import json
from io import BytesIO, StringIO
from typing import Any

from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.models import BitrixPreparedAction, CallResultImport, CallResultImportRow
from app.repositories.call_result_repository import CallResultRepository
from app.services.call_results.business_groups import count_business_groups, get_business_group


def _csv_safe(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@"):
        return "'" + text
    return text


class CallResultExportService:
    def __init__(self, db: Session, portal_id: str):
        self.repo = CallResultRepository(db, portal_id)

    def export_json(self, imp: CallResultImport) -> dict[str, Any]:
        rows = self.repo.list_rows(imp.id)
        actions = self.repo.list_actions(imp.id)
        by_method: dict[str, int] = {}
        for a in actions:
            if a.is_enabled:
                by_method[a.method] = by_method.get(a.method, 0) + 1

        return {
            "import": {
                "id": imp.id,
                "filename": imp.original_filename,
                "campaign_name": imp.campaign_name,
                "source_format": imp.source_format,
                "batch_id": imp.batch_id,
                "created_at": imp.created_at.isoformat() if imp.created_at else None,
                "total_rows": imp.total_rows,
            },
            "summary": {
                "comments": by_method.get("crm.timeline.comment.add", 0),
                "todos": by_method.get("crm.activity.todo.add", 0),
                "tasks": by_method.get("tasks.task.add", 0),
                "manual_review": imp.review_rows,
                "skipped": imp.skipped_rows,
                "business_groups": count_business_groups(rows),
            },
            "operations": [
                {
                    "source_row": a.import_row_id,
                    "action_group_id": a.action_group_id,
                    "method": a.method,
                    "payload": a.payload,
                    "validation_status": a.validation_status,
                    "is_enabled": a.is_enabled,
                    "source_identity": self._row_identity(rows, a),
                    "business_group": self._row_business_group(rows, a)[0],
                    "business_group_label": self._row_business_group(rows, a)[1],
                }
                for a in actions
            ],
        }

    def export_xlsx_bytes(self, imp: CallResultImport) -> bytes:
        rows = self.repo.list_rows(imp.id)
        wb = Workbook()
        ws = wb.active
        ws.title = "Review"
        headers = [
            "Строка", "Телефон", "Категория", "Код бизнес-группы", "Бизнес-группа",
            "Источник", "Уверенность",
            "Match", "Сделка", "Причина", "LLM статус", "Skip",
        ]
        ws.append(headers)
        for r in rows:
            ws.append([
                r.source_row_number,
                r.raw_phone,
                r.final_category,
                get_business_group(r)[0],
                get_business_group(r)[1],
                r.classification_source,
                r.llm_confidence,
                r.match_status,
                r.matched_deal_id,
                r.classification_reason or r.match_reason,
                r.llm_status,
                r.skip_reason,
            ])
        buf = BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def export_csv_bytes(self, imp: CallResultImport) -> bytes:
        rows = self.repo.list_rows(imp.id)
        row_by_id = {r.id: r for r in rows}
        actions = self.repo.list_actions(imp.id)
        buf = StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow([
            "batch_id", "phone", "technical_status", "call_result", "category",
            "business_group", "business_group_label",
            "deal_id", "responsible", "summary", "next_action", "callback_at",
            "method", "payload", "validation_status", "manual_override", "source_identity",
        ])
        for a in actions:
            row = row_by_id.get(a.import_row_id)
            ext = (row.extracted_data or {}) if row else {}
            writer.writerow([
                _csv_safe(imp.batch_id),
                _csv_safe(row.raw_phone if row else ""),
                _csv_safe(row.technical_status if row else ""),
                _csv_safe(row.call_result_display if row else ""),
                _csv_safe(row.final_category if row else ""),
                _csv_safe(get_business_group(row)[0] if row else ""),
                _csv_safe(get_business_group(row)[1] if row else ""),
                _csv_safe(row.matched_deal_id if row else ""),
                _csv_safe(""),
                _csv_safe(ext.get("summary", "")),
                _csv_safe(ext.get("next_action", "")),
                _csv_safe(row.callback_at.isoformat() if row and row.callback_at else ""),
                _csv_safe(a.method),
                _csv_safe(json.dumps(a.payload, ensure_ascii=False)),
                _csv_safe(a.validation_status),
                _csv_safe(row.manually_overridden if row else False),
                _csv_safe(row.source_identity if row else ""),
            ])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    @staticmethod
    def _row_business_group(
        rows: list[CallResultImportRow],
        action: BitrixPreparedAction,
    ) -> tuple[str | None, str | None]:
        for row in rows:
            if row.id == action.import_row_id:
                return get_business_group(row)
        return None, None

    @staticmethod
    def _row_identity(rows: list[CallResultImportRow], action: BitrixPreparedAction) -> str | None:
        for r in rows:
            if r.id == action.import_row_id:
                return r.source_identity
        return None
