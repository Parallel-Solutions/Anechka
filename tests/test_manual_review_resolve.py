"""Tests for manual review resolve API."""

from __future__ import annotations

import pytest

from app.models import (
    CallRetryQueueEntry,
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    ENTITY_DEAL,
)
from app.services.call_results.fake_classifier import (
    FakeCallResultClassifier,
    hot_lead_result,
)
from app.services.call_results.orchestrator import CallResultOrchestrator

PORTAL = "example.bitrix24.ru"

HOT_ROW_CSV = (
    "phone,comment,category,transcript,called_at,deal_id,call_id\n"
    '89161234567,"Need KP",hot_lead,"Client confirmed",2026-06-29T10:00:00+03:00,1001,call-001\n'
).encode("utf-8")


def _seed_crm(db, *, deal_assigned: dict[int, int | None] | None = None):
    assigned = deal_assigned or {}
    for deal_id, phone in [(1001, "89161234567"), (1002, "89161234568")]:
        db.add(
            CrmEntity(
                portal_id=PORTAL,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                title=f"Deal {deal_id}",
                assigned_by_id=assigned.get(deal_id, 42),
                raw_payload={"closed": "N"},
                payload_hash=f"hash-{deal_id}",
            )
        )
        cid = deal_id + 5000
        db.add(CrmContact(portal_id=PORTAL, contact_id=cid, full_name=f"C{cid}"))
        db.add(
            CrmContactPhone(
                portal_id=PORTAL,
                contact_id=cid,
                value=phone,
                value_type="MOBILE",
                is_primary=True,
            )
        )
        db.add(
            CrmContactLink(
                portal_id=PORTAL,
                contact_id=cid,
                parent_entity_type_id=ENTITY_DEAL,
                parent_entity_id=deal_id,
                is_primary=True,
            )
        )
    db.commit()


def _seed_second_lpr_contact(db, deal_id: int = 1001, phone: str = "89169999999"):
    cid = 6099
    db.add(
        CrmContact(
            portal_id=PORTAL,
            contact_id=cid,
            full_name="Генеральный директор",
            post="Генеральный директор",
        )
    )
    db.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=cid,
            value=phone,
            value_type="MOBILE",
            is_primary=True,
        )
    )
    db.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=cid,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=deal_id,
            is_primary=False,
        )
    )
    db.commit()


def _process_and_flag_manual(db_session):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier([hot_lead_result()]),
    )
    imp, _ = orch.save_uploaded_file(HOT_ROW_CSV, "demo.csv")
    db_session.commit()
    orch.process_import(imp.id)
    row = orch.repo.list_rows(imp.id)[0]
    row.needs_manual_review = True
    row.execution_status = "blocked_manual_review"
    row.manual_review_reason = "Тестовая ручная проверка"
    db_session.commit()
    return imp, row


def _resolve(client, import_id: int, row_id: int, action: str, **extra):
    return client.post(
        f"/api/call-results/imports/{import_id}/rows/{row_id}/manual-resolve",
        json={"action": action, "confirmed": True, **extra},
    )


def _preview(client, import_id: int, row_id: int, action: str):
    return client.post(
        f"/api/call-results/imports/{import_id}/rows/{row_id}/manual-preview",
        json={"action": action},
    )


def _row_with_transcript(db_session, imp, row):
    nd = dict(row.normalized_data or {})
    nd["scenario_events"] = [
        {
            "field": "Вход",
            "transcription": "Вы позвонили в администрацию",
        },
        {
            "field": "Выход на лпр",
            "transcription": "отдел архитектуры здравствуйте",
        },
    ]
    row.normalized_data = nd
    db_session.commit()
    return row


def test_manual_resolve_comment_prepares_action(client, db_session):
    imp, row = _process_and_flag_manual(db_session)
    resp = _resolve(client, imp.id, row.id, "comment")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "comment"
    assert data["prepared_method"] == "crm.timeline.comment.add"
    assert "Отправить в Bitrix24" in data["message"]
    assert "crm.timeline.comment.add" in data["prepared_methods"]
    assert data["execution_enabled"] is False

    db_session.refresh(row)
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"
    from app.repositories.call_result_repository import CallResultRepository

    repo = CallResultRepository(db_session, PORTAL)
    actions = [a for a in repo.list_actions(imp.id) if a.import_row_id == row.id]
    assert any(a.method == "crm.timeline.comment.add" for a in actions)


def test_manual_resolve_todo_without_responsible(client, db_session):
    from app.config import get_settings

    _seed_crm(db_session, deal_assigned={1001: None})
    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier([hot_lead_result()]),
    )
    imp, _ = orch.save_uploaded_file(HOT_ROW_CSV, "demo.csv")
    db_session.commit()
    orch.process_import(imp.id)
    row = orch.repo.list_rows(imp.id)[0]
    row.needs_manual_review = True
    row.execution_status = "blocked_manual_review"
    db_session.commit()

    resp = _resolve(client, imp.id, row.id, "todo")
    assert resp.status_code == 400
    assert "ответствен" in resp.json()["detail"].lower()


def test_manual_resolve_todo_prepares_action(client, db_session):
    imp, row = _process_and_flag_manual(db_session)
    resp = _resolve(client, imp.id, row.id, "todo")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["prepared_method"] == "crm.activity.todo.add"

    db_session.refresh(row)
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"


def test_manual_resolve_find_contact_adds_retry_queue(client, db_session):
    imp, row = _process_and_flag_manual(db_session)
    _seed_second_lpr_contact(db_session)

    resp = _resolve(client, imp.id, row.id, "find_contact")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "find_contact"
    assert data["contact_id"] == 6099
    assert data["phone"]

    db_session.refresh(row)
    assert row.needs_manual_review is False
    assert row.execution_status == "completed"

    entry = db_session.get(CallRetryQueueEntry, data["retry_queue_entry_id"])
    assert entry is not None
    assert entry.reason == "hangup_replacement_contact"
    assert entry.phone_normalized in ("89169999999", "79169999999")


def test_manual_resolve_find_contact_no_candidate(client, db_session):
    imp, row = _process_and_flag_manual(db_session)
    resp = _resolve(client, imp.id, row.id, "find_contact")
    assert resp.status_code == 422


def test_manual_resolve_prepared_row(client, db_session):
    from app.config import get_settings
    from app.repositories.call_result_repository import CallResultRepository

    _seed_crm(db_session)
    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier([hot_lead_result()]),
    )
    imp, _ = orch.save_uploaded_file(HOT_ROW_CSV, "demo.csv")
    db_session.commit()
    orch.process_import(imp.id)
    row = orch.repo.list_rows(imp.id)[0]
    assert not row.needs_manual_review
    assert row.execution_status == "prepared"

    resp = _resolve(client, imp.id, row.id, "comment")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "comment"
    assert data["prepared_method"] == "crm.timeline.comment.add"

    db_session.refresh(row)
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"
    assert row.classification_source == "manual"

    repo = CallResultRepository(db_session, PORTAL)
    actions = [a for a in repo.list_actions(imp.id) if a.import_row_id == row.id]
    assert any(a.method == "crm.timeline.comment.add" for a in actions)


def test_manual_resolve_without_deal(client, db_session):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier([hot_lead_result()]),
    )
    imp, _ = orch.save_uploaded_file(HOT_ROW_CSV, "demo.csv")
    db_session.commit()
    orch.process_import(imp.id)
    row = orch.repo.list_rows(imp.id)[0]
    row.needs_manual_review = True
    row.execution_status = "blocked_manual_review"
    row.matched_deal_id = None
    row.matched_deal_local_id = None
    row.match_status = "not_found"
    db_session.commit()

    resp = _resolve(client, imp.id, row.id, "comment")
    assert resp.status_code == 400
    assert "сделка" in resp.json()["detail"].lower()


def _enable_contact_creation(monkeypatch):
    monkeypatch.setenv("BITRIX_CALL_SOURCE_FIELD_CODE", "UF_CRM_CALL_SOURCE")
    monkeypatch.setenv("BITRIX_CALL_SOURCE_FIELD_VALUE", "anechka")
    from app.config import get_settings

    get_settings.cache_clear()


def _manual_row_without_deal(db_session):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier([hot_lead_result()]),
    )
    imp, _ = orch.save_uploaded_file(HOT_ROW_CSV, "demo.csv")
    db_session.commit()
    orch.process_import(imp.id)
    row = orch.repo.list_rows(imp.id)[0]
    row.needs_manual_review = True
    row.execution_status = "blocked_manual_review"
    row.matched_deal_id = None
    row.matched_deal_local_id = None
    row.match_status = "not_found"
    row.manual_review_reason = "Телефон не найден"
    db_session.commit()
    return imp, row


def test_manual_resolve_create_contact_without_deal(client, db_session, monkeypatch):
    _enable_contact_creation(monkeypatch)
    imp, row = _manual_row_without_deal(db_session)

    resp = _resolve(client, imp.id, row.id, "create_contact")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "create_contact"
    assert data["prepared_method"] == "crm.contact.add"

    db_session.refresh(row)
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"

    from app.repositories.call_result_repository import CallResultRepository

    repo = CallResultRepository(db_session, PORTAL)
    actions = [a for a in repo.list_actions(imp.id) if a.import_row_id == row.id]
    methods = [a.method for a in actions]
    assert "crm.contact.list" in methods
    assert "crm.contact.add" in methods
    assert "crm.deal.contact.add" not in methods


def test_manual_resolve_create_contact_with_deal(client, db_session, monkeypatch):
    _enable_contact_creation(monkeypatch)
    imp, row = _process_and_flag_manual(db_session)

    resp = _resolve(client, imp.id, row.id, "create_contact")
    assert resp.status_code == 200, resp.text

    from app.repositories.call_result_repository import CallResultRepository

    repo = CallResultRepository(db_session, PORTAL)
    actions = [a for a in repo.list_actions(imp.id) if a.import_row_id == row.id]
    methods = [a.method for a in actions]
    assert "crm.contact.add" in methods
    assert "crm.deal.contact.add" in methods


def test_manual_resolve_create_contact_no_phone(client, db_session, monkeypatch):
    _enable_contact_creation(monkeypatch)
    imp, row = _manual_row_without_deal(db_session)
    row.raw_phone = ""
    row.normalized_phone = None
    db_session.commit()

    resp = _resolve(client, imp.id, row.id, "create_contact")
    assert resp.status_code == 400
    assert "телефон" in resp.json()["detail"].lower()


def test_manual_preview_comment_returns_transcript(client, db_session):
    imp, row = _process_and_flag_manual(db_session)
    row = _row_with_transcript(db_session, imp, row)

    resp = _preview(client, imp.id, row.id, "comment")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "comment"
    assert "отдел архитектуры" in data["preview_text"]


def test_manual_preview_todo_llm_disabled_fallback(client, db_session, monkeypatch):
    monkeypatch.setenv("LLM_CALL_RESULTS_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()

    imp, row = _process_and_flag_manual(db_session)
    row = _row_with_transcript(db_session, imp, row)

    resp = _preview(client, imp.id, row.id, "todo")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["action"] == "todo"
    assert data["todo_title"]
    assert "отдел архитектуры" in data["preview_text"]


def test_manual_preview_find_contact_keyword_match(client, db_session, monkeypatch):
    from app.services.call_results.manual_review_ai_service import (
        AiExtractOutcome,
        SearchKeywordsResult,
    )

    imp, row = _process_and_flag_manual(db_session)
    row = _row_with_transcript(db_session, imp, row)
    db_session.add(
        CrmContact(
            portal_id=PORTAL,
            contact_id=6101,
            full_name="Архитектор Иванов",
            post="отдел архитектуры",
        )
    )
    db_session.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=6101,
            value="89161112233",
            value_type="MOBILE",
            is_primary=True,
        )
    )
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=6101,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=1001,
            is_primary=False,
        )
    )
    db_session.commit()

    def fake_keywords(self, transcript):
        return AiExtractOutcome(keywords=SearchKeywordsResult(keywords=["архитектур"], confidence=0.9))

    monkeypatch.setattr(
        "app.services.call_results.manual_review_service.ManualReviewAiService.extract_search_keywords",
        fake_keywords,
    )

    resp = _preview(client, imp.id, row.id, "find_contact")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["search_method"] == "ai_keywords"
    assert data["found_contact"]["contact_id"] == 6101
    assert data["ai_keywords"] == ["архитектур"]


def test_manual_preview_find_contact_lpr_fallback(client, db_session, monkeypatch):
    from app.services.call_results.manual_review_ai_service import AiExtractOutcome

    imp, row = _process_and_flag_manual(db_session)
    _seed_second_lpr_contact(db_session)

    def fake_keywords(self, transcript):
        return AiExtractOutcome(error_type="disabled", error_message="LLM недоступна")

    monkeypatch.setattr(
        "app.services.call_results.manual_review_service.ManualReviewAiService.extract_search_keywords",
        fake_keywords,
    )

    resp = _preview(client, imp.id, row.id, "find_contact")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["search_method"] == "lpr_fallback"
    assert data["found_contact"]["contact_id"] == 6099


def test_manual_resolve_comment_with_override(client, db_session):
    imp, row = _process_and_flag_manual(db_session)
    row = _row_with_transcript(db_session, imp, row)

    preview = _preview(client, imp.id, row.id, "comment")
    assert preview.status_code == 200

    custom_text = "Комментарий оператора из превью"
    resp = _resolve(client, imp.id, row.id, "comment", preview_text=custom_text)
    assert resp.status_code == 200, resp.text

    from app.repositories.call_result_repository import CallResultRepository

    repo = CallResultRepository(db_session, PORTAL)
    actions = [a for a in repo.list_actions(imp.id) if a.import_row_id == row.id]
    comment_action = next(a for a in actions if a.method == "crm.timeline.comment.add")
    assert custom_text in comment_action.payload["fields"]["COMMENT"]


def test_manual_resolve_create_contact_with_ai_data(client, db_session, monkeypatch):
    from app.services.call_results.llm_schema import AlternateContactData
    from app.services.call_results.manual_review_ai_service import AiExtractOutcome

    _enable_contact_creation(monkeypatch)
    imp, row = _process_and_flag_manual(db_session)
    row = _row_with_transcript(db_session, imp, row)

    def fake_contact(self, transcript):
        return AiExtractOutcome(
            contact=AlternateContactData(
                name="Петр Архитектор",
                phone="89160001122",
                position="отдел архитектуры",
            )
        )

    monkeypatch.setattr(
        "app.services.call_results.manual_review_service.ManualReviewAiService.extract_contact_data",
        fake_contact,
    )

    preview = _preview(client, imp.id, row.id, "create_contact")
    assert preview.status_code == 200, preview.text
    data = preview.json()
    assert data["contact_data"]["name"] == "Петр Архитектор"

    resp = _resolve(
        client,
        imp.id,
        row.id,
        "create_contact",
        contact_data=data["contact_data"],
    )
    assert resp.status_code == 200, resp.text

    from app.repositories.call_result_repository import CallResultRepository

    repo = CallResultRepository(db_session, PORTAL)
    actions = [a for a in repo.list_actions(imp.id) if a.import_row_id == row.id]
    create_action = next(a for a in actions if a.method == "crm.contact.add")
    assert create_action.payload["contact"]["name"] == "Петр Архитектор"
    assert create_action.payload["contact"]["phone"] == "89160001122"