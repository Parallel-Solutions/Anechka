"""Tests for call result rematch."""

from app.models import (
    CallResultImport,
    CallResultImportRow,
    CrmEntity,
    ENTITY_COMPANY,
    ENTITY_DEAL,
)
from app.services.call_results.fake_classifier import FakeCallResultClassifier
from app.services.call_results.orchestrator import CallResultOrchestrator

PORTAL = "example.bitrix24.ru"


def _seed_company_deal(db):
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_COMPANY,
            entity_id=6591,
            title="Company",
            raw_payload={"phone": "83533534219"},
            payload_hash="hash-co",
        )
    )
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=24146,
            title="Deal",
            assigned_by_id=42,
            raw_payload={"closed": "N", "companyId": 6591},
            payload_hash="hash-deal",
        )
    )


def test_rematch_import_updates_not_found(db_session):
    from app.config import get_settings

    _seed_company_deal(db_session)
    imp = CallResultImport(
        portal_id=PORTAL,
        source_format="generic",
        status="completed",
        original_filename="test.csv",
        storage_key="test/test.csv",
        file_sha256="abc",
    )
    db_session.add(imp)
    db_session.flush()
    row = CallResultImportRow(
        import_id=imp.id,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        normalized_phone="73533534219",
        match_status="not_found",
        match_reason="Телефон не найден",
        llm_status="not_required",
    )
    db_session.add(row)
    db_session.commit()

    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier(responses=[]),
    )
    updated = orch.rematch_import(imp.id)
    db_session.refresh(row)

    assert updated == 1
    assert row.match_status == "matched"
    assert row.matched_deal_id == 24146
    assert row.matched_company_id == 6591
