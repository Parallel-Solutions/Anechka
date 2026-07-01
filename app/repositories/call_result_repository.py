"""Repository for call result imports."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    BitrixPreparedAction,
    CallResultImport,
    CallResultImportRow,
    CallResultLlmCache,
    utcnow,
)


class CallResultRepository:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id

    def create_import(
        self,
        *,
        original_filename: str,
        storage_key: str,
        file_sha256: str,
        file_size: int,
        created_by: str | None = None,
        duplicate_of_import_id: int | None = None,
        selected_sheet: str | None = None,
        column_mapping: dict | None = None,
    ) -> CallResultImport:
        rec = CallResultImport(
            portal_id=self.portal_id,
            original_filename=original_filename,
            storage_key=storage_key,
            file_sha256=file_sha256,
            file_size=file_size,
            created_by=created_by,
            duplicate_of_import_id=duplicate_of_import_id,
            selected_sheet=selected_sheet,
            column_mapping=column_mapping,
            status="uploaded",
        )
        self.db.add(rec)
        self.db.flush()
        return rec

    def get_import(self, import_id: int) -> CallResultImport | None:
        return self.db.scalar(
            select(CallResultImport).where(
                CallResultImport.id == import_id,
                CallResultImport.portal_id == self.portal_id,
            )
        )

    def list_imports(self, limit: int = 20) -> list[CallResultImport]:
        return list(
            self.db.scalars(
                select(CallResultImport)
                .where(CallResultImport.portal_id == self.portal_id)
                .order_by(CallResultImport.created_at.desc())
                .limit(limit)
            )
        )

    def find_by_sha256(self, file_sha256: str) -> CallResultImport | None:
        return self.db.scalar(
            select(CallResultImport)
            .where(
                CallResultImport.portal_id == self.portal_id,
                CallResultImport.file_sha256 == file_sha256,
            )
            .order_by(CallResultImport.created_at.desc())
            .limit(1)
        )

    def update_import(self, import_id: int, **fields: Any) -> CallResultImport | None:
        rec = self.get_import(import_id)
        if rec is None:
            return None
        for k, v in fields.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        self.db.flush()
        return rec

    def bulk_insert_rows(self, rows: list[CallResultImportRow]) -> None:
        self.db.add_all(rows)
        self.db.flush()

    def get_row(self, import_id: int, row_id: int) -> CallResultImportRow | None:
        return self.db.scalar(
            select(CallResultImportRow).where(
                CallResultImportRow.id == row_id,
                CallResultImportRow.import_id == import_id,
            )
        )

    def list_rows(self, import_id: int) -> list[CallResultImportRow]:
        return list(
            self.db.scalars(
                select(CallResultImportRow)
                .where(CallResultImportRow.import_id == import_id)
                .order_by(CallResultImportRow.source_row_number)
            )
        )

    def list_actions(self, import_id: int) -> list[BitrixPreparedAction]:
        return list(
            self.db.scalars(
                select(BitrixPreparedAction)
                .where(BitrixPreparedAction.import_id == import_id)
                .order_by(BitrixPreparedAction.id)
            )
        )

    def get_action(self, import_id: int, action_id: int) -> BitrixPreparedAction | None:
        return self.db.scalar(
            select(BitrixPreparedAction).where(
                BitrixPreparedAction.id == action_id,
                BitrixPreparedAction.import_id == import_id,
            )
        )

    def delete_actions_for_row(self, row_id: int, *, preserve_user_modified: bool = False) -> None:
        if preserve_user_modified:
            from sqlalchemy import delete, and_
            self.db.execute(
                delete(BitrixPreparedAction).where(
                    and_(
                        BitrixPreparedAction.import_row_id == row_id,
                        BitrixPreparedAction.user_modified.is_(False),
                    )
                )
            )
        else:
            self.db.execute(delete(BitrixPreparedAction).where(BitrixPreparedAction.import_row_id == row_id))

    def delete_actions_for_import(self, import_id: int) -> None:
        self.db.execute(delete(BitrixPreparedAction).where(BitrixPreparedAction.import_id == import_id))

    def delete_import(self, import_id: int) -> bool:
        rec = self.get_import(import_id)
        if rec is None:
            return False
        self.db.delete(rec)
        self.db.flush()
        return True

    def get_llm_cache(self, input_hash: str) -> CallResultLlmCache | None:
        return self.db.scalar(
            select(CallResultLlmCache).where(
                CallResultLlmCache.portal_id == self.portal_id,
                CallResultLlmCache.input_hash == input_hash,
            )
        )

    def upsert_llm_cache(
        self,
        *,
        input_hash: str,
        prompt_version: str,
        schema_version: str,
        model: str,
        result_json: dict,
        confidence: float,
        token_usage: dict | None,
    ) -> CallResultLlmCache:
        existing = self.get_llm_cache(input_hash)
        now = utcnow()
        if existing is None:
            existing = CallResultLlmCache(
                portal_id=self.portal_id,
                input_hash=input_hash,
                prompt_version=prompt_version,
                schema_version=schema_version,
                model=model,
                result_json=result_json,
                confidence=confidence,
                token_usage=token_usage,
                created_at=now,
                last_used_at=now,
            )
            self.db.add(existing)
        else:
            existing.prompt_version = prompt_version
            existing.schema_version = schema_version
            existing.model = model
            existing.result_json = result_json
            existing.confidence = confidence
            existing.token_usage = token_usage
            existing.last_used_at = now
            existing.use_count += 1
        return existing

    def count_rows_by_llm_status(self, import_id: int) -> dict[str, int]:
        rows = self.db.execute(
            select(CallResultImportRow.llm_status, func.count())
            .where(CallResultImportRow.import_id == import_id)
            .group_by(CallResultImportRow.llm_status)
        ).all()
        return {status: count for status, count in rows}
