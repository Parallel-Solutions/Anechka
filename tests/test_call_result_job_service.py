"""Tests for call result background job service."""

from __future__ import annotations

from unittest.mock import patch

from app.models.call_results import CallResultImport, CallResultImportRow
from app.services.call_results.job_service import CallResultJobService

PORTAL = "example.bitrix24.ru"


def _make_import(db_session, *, status: str = "processing", with_row: bool = True) -> CallResultImport:
    imp = CallResultImport(
        portal_id=PORTAL,
        original_filename="demo.csv",
        storage_key="demo.csv",
        file_sha256="abc123",
        status=status,
        selected_sheet="Sheet1",
        column_mapping={"phone": "Phone"},
    )
    db_session.add(imp)
    db_session.flush()
    if with_row:
        db_session.add(
            CallResultImportRow(
                import_id=imp.id,
                source_row_number=2,
                llm_required=True,
                llm_status="pending",
            )
        )
    db_session.commit()
    db_session.refresh(imp)
    return imp


def test_recover_interrupted_imports_retries_llm_when_rows_exist(db_session):
    imp = _make_import(db_session)
    submitted: list[tuple[int, dict]] = []

    def capture_submit(self, import_id: int, **kwargs):
        submitted.append((import_id, kwargs))

    with patch.object(CallResultJobService, "submit_process", capture_submit):
        CallResultJobService.recover_interrupted_imports(db_session)

    assert submitted == [(imp.id, {"retry_llm_only": True})]


def test_recover_interrupted_imports_reparses_when_no_rows(db_session):
    imp = _make_import(db_session, with_row=False)
    submitted: list[tuple[int, dict]] = []

    def capture_submit(self, import_id: int, **kwargs):
        submitted.append((import_id, kwargs))

    with patch.object(CallResultJobService, "submit_process", capture_submit):
        CallResultJobService.recover_interrupted_imports(db_session)

    assert submitted == [
        (
            imp.id,
            {
                "sheet_name": "Sheet1",
                "column_mapping": {"phone": "Phone"},
            },
        )
    ]


def test_recover_interrupted_imports_noop_when_none_processing(db_session):
    _make_import(db_session, status="ready")
    submitted: list[tuple[int, dict]] = []

    def capture_submit(self, import_id: int, **kwargs):
        submitted.append((import_id, kwargs))

    with patch.object(CallResultJobService, "submit_process", capture_submit):
        CallResultJobService.recover_interrupted_imports(db_session)

    assert submitted == []
