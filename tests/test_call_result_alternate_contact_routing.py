"""Orchestrator routing tests for alternate_contact_requested."""

from __future__ import annotations

from app.config import get_settings
from app.models import CallResultImport, CallResultImportRow, CrmContact, CrmContactLink, CrmContactPhone, CrmEntity, ENTITY_DEAL
from app.services.call_results.fake_classifier import FakeCallResultClassifier
from app.services.call_results.llm_schema import CallResultLLMResult
from app.services.call_results.orchestrator import CallResultOrchestrator
from app.services.call_results.row_disposition import get_row_disposition
from app.services.call_results.row_filter import get_row_filter

PORTAL = "example.bitrix24.ru"
DEAL_ID = 1001
SOURCE_PHONE = "89161234567"
ALT_PHONE = "89001234567"


def _seed_deal_with_alternate_contact(db, *, include_alternate: bool):
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=DEAL_ID,
            title="Deal alternate",
            assigned_by_id=42,
            raw_payload={"closed": "N"},
            payload_hash="hash-alt",
        )
    )
    source_cid = 5001
    db.add(CrmContact(portal_id=PORTAL, contact_id=source_cid, full_name="Source"))
    db.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=source_cid,
            value=SOURCE_PHONE,
            value_type="MOBILE",
            is_primary=True,
        )
    )
    db.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=source_cid,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=DEAL_ID,
            is_primary=True,
        )
    )
    if include_alternate:
        alt_cid = 9001
        db.add(CrmContact(portal_id=PORTAL, contact_id=alt_cid, full_name="Alternate"))
        db.add(
            CrmContactPhone(
                portal_id=PORTAL,
                contact_id=alt_cid,
                value=ALT_PHONE,
                value_type="MOBILE",
                is_primary=True,
            )
        )
    db.commit()


def _alternate_llm_result() -> dict:
    return CallResultLLMResult.model_validate(
        {
            "alternate_contact_requested": True,
            "alternate_contact": {
                "name": "Иван",
                "phone": ALT_PHONE,
                "extension": None,
                "email": None,
                "position": None,
            },
            "summary": "Дали другой номер",
            "confidence": 0.88,
            "signal_reasons": {"alternate_contact_requested": "Новый контакт"},
        }
    ).model_dump()


def _finalize_alternate_row(db_session, *, include_alternate: bool):
    _seed_deal_with_alternate_contact(db_session, include_alternate=include_alternate)
    settings = get_settings()
    orch = CallResultOrchestrator(db_session, settings, PORTAL, FakeCallResultClassifier([]))
    imp = CallResultImport(
        portal_id=PORTAL,
        original_filename="alternate.csv",
        storage_key="alternate.csv",
        file_sha256="altsha",
        status="ready",
    )
    db_session.add(imp)
    db_session.flush()
    row = CallResultImportRow(
        import_id=imp.id,
        source_row_number=2,
        raw_data={},
        normalized_data={
            "phone": SOURCE_PHONE,
            "call_result": "Fully Completed",
            "has_meaningful_content": True,
            "scenario_events": [
                {"field": "Выход на лпр", "match": "Иван"},
                {"field": "номер ЛПР", "match": ALT_PHONE},
            ],
        },
        raw_phone=SOURCE_PHONE,
        normalized_phone=SOURCE_PHONE,
        match_status="matched",
        matched_deal_id=DEAL_ID,
        matched_contact_id=5001,
        llm_status="completed",
        llm_required=True,
        llm_result=_alternate_llm_result(),
        needs_manual_review=False,
        execution_status="pending",
    )
    db_session.add(row)
    db_session.commit()
    orch.matcher.build_indexes()
    orch._finalize_row(row, imp)
    db_session.commit()
    actions = [a for a in orch.repo.list_actions(imp.id) if a.import_row_id == row.id]
    return row, actions


def test_alternate_contact_found_plans_link_and_retry(db_session):
    row, actions = _finalize_alternate_row(db_session, include_alternate=True)

    assert (row.business_signals or {}).get("alternate_contact_requested")
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"
    assert (row.extracted_data or {}).get("alternate_contact_id") == 9001

    methods = [a.method for a in actions if a.is_enabled]
    assert methods == ["crm.deal.contact.add", "retry_queue.add"]
    assert actions[0].payload.get("contact_id") == 9001
    assert actions[1].payload.get("reason") == "alternate_contact"
    assert get_row_filter(row, actions) == "new_contacts"
    assert get_row_disposition(row, actions) == "auto_call"


def test_alternate_contact_not_found_goes_to_manual_review(db_session):
    row, actions = _finalize_alternate_row(db_session, include_alternate=False)

    assert (row.business_signals or {}).get("alternate_contact_requested")
    assert row.needs_manual_review is True
    assert row.execution_status == "blocked_manual_review"
    assert "не найден" in (row.manual_review_reason or "").lower()
    assert actions == []
    assert get_row_disposition(row, actions) == "manual_review"
