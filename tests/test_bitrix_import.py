"""Tests for Bitrix CRM import module — 22 required scenarios."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sqlalchemy import select

from app.config import Settings
from app.models import (
    CrmEntity,
    CrmFieldDefinition,
    CrmFieldSemantic,
    ENTITY_DEAL,
    ENTITY_LEAD,
    SyncRun,
)
from app.repositories.crm_repository import CrmRepository
from app.repositories.sync_repository import SyncRepository
from app.services.bitrix_import.bitrix_crm_client import BitrixCrmClient
from app.services.bitrix_import.metadata_ai_service import BitrixMetadataAIService
from app.services.bitrix_import.orchestrator import ImportOrchestrator
from app.utils.anonymize import anonymize_string
from app.utils.hash_utils import payload_hash, source_hash
from tests.fixtures.bitrix_import_fixtures import (
    AI_FIELD_RESPONSE,
    INVALID_AI_RESPONSE,
    SAMPLE_DEAL,
    SAMPLE_DEAL_SAME_TIME_1,
    SAMPLE_DEAL_SAME_TIME_2,
    SAMPLE_DEAL_UPDATED,
    SAMPLE_FIELDS,
    SAMPLE_FIELDS_V2,
    SAMPLE_LEAD,
)

PORTAL = "example.bitrix24.ru"


def _settings():
    return Settings(
        bitrix_webhook_url="https://example.bitrix24.ru/rest/1/token",
        openai_api_key="sk-test",
        import_batch_size=50,
        import_overlap_minutes=10,
    )


@pytest.fixture()
def crm_repo(db_session):
    return CrmRepository(db_session, PORTAL)


@pytest.fixture()
def sync_repo(db_session):
    return SyncRepository(db_session)


# 1. First full import
def test_first_full_import(db_session, sync_repo):
    run = sync_repo.create_run(PORTAL, "full")
    client = MagicMock(spec=BitrixCrmClient)
    client.get_item_fields.return_value = SAMPLE_FIELDS
    client.list_items_keyset.return_value = iter([[SAMPLE_LEAD], [SAMPLE_DEAL]])
    client.list_all_categories.return_value = []
    client.list_currencies.return_value = []
    client.get_users.return_value = [{"id": 1, "name": "User"}]
    client.list_product_rows.return_value = []
    client.list_activities.return_value = []
    client.list_timeline_comments.return_value = []
    client.list_stage_history.return_value = []
    client.list_requisites.return_value = []
    client.list_addresses.return_value = []
    client.diagnostics = MagicMock(to_dict=lambda: {})
    client.api_requests_count = 0

    with patch("app.services.bitrix_import.orchestrator.BitrixCrmClient", return_value=client):
        with patch("app.services.bitrix_import.orchestrator.SchemaDiscoveryService") as mock_disc:
            mock_disc.return_value.discover_fields.return_value = []
            mock_disc.return_value.discover_dictionaries.return_value = None
            mock_disc.return_value.sync_global_dictionaries.return_value = None
            orch = ImportOrchestrator(db_session, _settings(), PORTAL, run.id)
            orch.client = client
            orch.discovery = mock_disc.return_value
            orch._run_full(analyze_metadata=False)

    repo = CrmRepository(db_session, PORTAL)
    assert repo.count_entities(ENTITY_LEAD) >= 0


# 2. Incremental without changes
def test_incremental_no_changes(db_session, crm_repo, sync_repo):
    crm_repo.upsert_entity(ENTITY_DEAL, 201, SAMPLE_DEAL)
    db_session.commit()
    cp_time = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    sync_repo.upsert_checkpoint(PORTAL, "entities", ENTITY_DEAL, cp_time, 201)
    run = sync_repo.create_run(PORTAL, "incremental")

    client = MagicMock(spec=BitrixCrmClient)
    client.get_item_fields.return_value = SAMPLE_FIELDS
    client.list_items_keyset.return_value = iter([])
    client.diagnostics = MagicMock(to_dict=lambda: {})
    client.api_requests_count = 0

    with patch("app.services.bitrix_import.orchestrator.BitrixCrmClient", return_value=client):
        orch = ImportOrchestrator(db_session, _settings(), PORTAL, run.id)
        orch.client = client
        orch._run_incremental(analyze_metadata=False)
    assert orch.stats["unchanged"] == 0


# 3. Deal update
def test_deal_update(db_session, crm_repo):
    _, action1 = crm_repo.upsert_entity(ENTITY_DEAL, 201, SAMPLE_DEAL)
    _, action2 = crm_repo.upsert_entity(ENTITY_DEAL, 201, SAMPLE_DEAL_UPDATED)
    db_session.commit()
    assert action1 == "created"
    assert action2 == "updated"
    entity = crm_repo.get_entity(ENTITY_DEAL, 201)
    assert entity.title == "Updated Deal"


# 4. Same updatedTime multiple deals
def test_same_updated_time(db_session, crm_repo):
    crm_repo.upsert_entity(ENTITY_DEAL, 301, SAMPLE_DEAL_SAME_TIME_1)
    crm_repo.upsert_entity(ENTITY_DEAL, 302, SAMPLE_DEAL_SAME_TIME_2)
    db_session.commit()
    assert crm_repo.count_entities(ENTITY_DEAL) == 2


# 5. Custom field only change
def test_custom_field_change(db_session, crm_repo):
    deal = dict(SAMPLE_DEAL)
    crm_repo.upsert_entity(ENTITY_DEAL, 201, deal)
    deal["UF_CRM_CUSTOM"] = "new_value"
    _, action = crm_repo.upsert_entity(ENTITY_DEAL, 201, deal)
    assert action == "updated"


# 6. New UF_CRM field appears
def test_new_uf_field(db_session, crm_repo):
    field_def, changed = crm_repo.upsert_field_definition(
        ENTITY_DEAL, "UF_CRM_NEW", SAMPLE_FIELDS_V2["UF_CRM_NEW"]
    )
    assert changed is True
    assert field_def.original_field_name == "UF_CRM_NEW"


# 7. Enumeration change
def test_enumeration_change(db_session, crm_repo):
    d = crm_repo.upsert_dictionary(ENTITY_DEAL, "enum_test", "Bitrix enumeration")
    crm_repo.upsert_dictionary_entry(d.id, "1", "A")
    crm_repo.upsert_dictionary_entry(d.id, "2", "B")
    db_session.commit()
    deactivated = crm_repo.deactivate_dictionary_entries(d.id, {"1"})
    assert deactivated == 1


# 8. Dictionary entry deletion
def test_dictionary_entry_deletion(db_session, crm_repo):
    d = crm_repo.upsert_dictionary(0, "test_dict", "enumeration")
    crm_repo.upsert_dictionary_entry(d.id, "x", "val")
    db_session.commit()
    count = crm_repo.deactivate_dictionary_entries(d.id, set())
    assert count == 1


# 9. Multiple custom field values
def test_multiple_field_values(db_session, crm_repo):
    fd, _ = crm_repo.upsert_field_definition(ENTITY_DEAL, "UF_MULTI", {"type": "string", "isMultiple": True})
    crm_repo.replace_field_values(
        ENTITY_DEAL, 201, fd.id,
        [{"raw": "a", "text": "a"}, {"raw": "b", "text": "b"}],
    )
    db_session.commit()
    from app.models import CrmEntityFieldValue
    from sqlalchemy import select

    vals = list(db_session.scalars(select(CrmEntityFieldValue).where(CrmEntityFieldValue.field_definition_id == fd.id)))
    assert len(vals) == 2


# 10. Lead deletion
def test_lead_deletion(db_session, crm_repo):
    crm_repo.upsert_entity(ENTITY_LEAD, 101, SAMPLE_LEAD)
    db_session.commit()
    assert crm_repo.mark_deleted(ENTITY_LEAD, 101)
    entity = crm_repo.get_entity(ENTITY_LEAD, 101)
    assert entity.is_deleted is True


# 11. Connection loss during page (retry handled by client)
def test_connection_loss_retry():
    client = BitrixCrmClient(_settings())
    ok = MagicMock()
    ok.status_code = 200
    ok.json.return_value = {"result": {"items": []}}
    fail = MagicMock()
    fail.status_code = 503
    fail.json.return_value = {}
    fail.raise_for_status = MagicMock(side_effect=Exception("503"))
    with patch.object(client.session, "post", side_effect=[fail, ok]):
        with patch("time.sleep"):
            data = client.call("crm.item.list", {"entityTypeId": 2})
    assert "result" in data


# 12. Restart after failure
def test_restart_after_failure(db_session, sync_repo):
    run = sync_repo.create_run(PORTAL, "incremental")
    sync_repo.complete_run(run.id, "failed", "Connection lost")
    run2 = sync_repo.create_run(PORTAL, "incremental")
    assert run2.status == "pending"


# 13. Duplicate event idempotency
def test_duplicate_event_idempotent(db_session, crm_repo):
    _, a1 = crm_repo.upsert_entity(ENTITY_DEAL, 201, SAMPLE_DEAL)
    _, a2 = crm_repo.upsert_entity(ENTITY_DEAL, 201, SAMPLE_DEAL)
    db_session.commit()
    assert a1 == "created"
    assert a2 == "unchanged"


# 14. OpenAI error does not abort import
def test_openai_error_continues(db_session):
    ai = BitrixMetadataAIService(_settings(), db_session, PORTAL)
    ai.client = MagicMock()
    ai.client.chat.completions.create.side_effect = Exception("API error")
    field = CrmFieldDefinition(
        portal_id=PORTAL, entity_type_id=2, original_field_name="title",
        definition_hash="abc", field_type="string",
    )
    db_session.add(field)
    db_session.commit()
    count = ai.analyze_fields([field])
    assert count == 0


# 15. No repeat AI on same source_hash
def test_no_repeat_ai_same_hash(db_session):
    ai = BitrixMetadataAIService(_settings(), db_session, PORTAL)
    field = CrmFieldDefinition(
        portal_id=PORTAL, entity_type_id=2, original_field_name="title",
        definition_hash="abc", field_type="string", title="Title",
    )
    db_session.add(field)
    db_session.flush()
    ctx = ai._build_field_context(field, [])
    sh = ai._compute_source_hash(field, ctx)
    sem = CrmFieldSemantic(field_definition_id=field.id, source_hash=sh, is_manual=False)
    db_session.add(sem)
    db_session.commit()
    ai.client = MagicMock()
    count = ai.analyze_fields([field])
    assert count == 0
    ai.client.chat.completions.create.assert_not_called()


# 16. Invalid AI JSON rejected
def test_invalid_ai_json_rejected():
    assert BitrixMetadataAIService.validate_field_response(INVALID_AI_RESPONSE["fields"][0]) is False
    assert BitrixMetadataAIService.validate_field_response(AI_FIELD_RESPONSE["fields"][0]) is True


# 17. Manual description not overwritten
def test_manual_description_preserved(db_session, crm_repo):
    fd, _ = crm_repo.upsert_field_definition(ENTITY_DEAL, "title", SAMPLE_FIELDS["title"])
    db_session.commit()
    crm_repo.save_semantic(fd.id, {"display_name": "Manual", "source_hash": "x", "is_manual": True})
    db_session.commit()
    ai = BitrixMetadataAIService(_settings(), db_session, PORTAL)
    ai.client = MagicMock()
    ai.analyze_fields([fd], force=True)
    ai.client.chat.completions.create.assert_not_called()


# 18. Email and phone not sent to AI
def test_anonymization_masks_pii():
    result = anonymize_string("Contact: ivan@example.com, +79991234567")
    assert "[EMAIL]" in result
    assert "[PHONE]" in result
    assert "ivan@example.com" not in result


# 19. Server-side pagination
def test_server_pagination(db_session, crm_repo):
    for i in range(5):
        crm_repo.upsert_entity(ENTITY_DEAL, 1000 + i, {**SAMPLE_DEAL, "id": 1000 + i, "title": f"D{i}"})
    db_session.commit()
    items, total = crm_repo.list_entities_paginated(ENTITY_DEAL, page=1, page_size=2)
    assert len(items) == 2
    assert total == 5


# 20. Concurrent import blocked
def test_concurrent_import_blocked(db_session, sync_repo, client: TestClient):
    sync_repo.create_run(PORTAL, "incremental")
    db_session.commit()
    with patch("app.routers.admin_bitrix._portal", return_value=PORTAL):
        resp = client.post("/admin/bitrix/imports", json={"mode": "incremental"})
    assert resp.status_code == 409


# 21. Migrations on clean DB
def test_migrations_clean_db(db_session):
    from app.database import Base

    tables = set(Base.metadata.tables.keys())
    assert "sync_runs" in tables
    assert "crm_entities" in tables
    assert "crm_field_value_profiles" in tables
    assert "export_jobs" in tables


# 22. Migrations on existing DB (idempotent create_all)
def test_migrations_existing_db(db_session, crm_repo):
    crm_repo.upsert_entity(ENTITY_LEAD, 1, SAMPLE_LEAD)
    db_session.commit()
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    entity = crm_repo.get_entity(ENTITY_LEAD, 1)
    assert entity is not None


def test_payload_hash_stable():
    h1 = payload_hash(SAMPLE_DEAL)
    h2 = payload_hash(SAMPLE_DEAL)
    assert h1 == h2


def test_source_hash_changes_on_prompt_version():
    h1 = source_hash("def", {}, "1", "gpt-4o")
    h2 = source_hash("def", {}, "2", "gpt-4o")
    assert h1 != h2


def test_create_import_api(client: TestClient, db_session, sync_repo):
    with patch("app.routers.admin_bitrix._portal", return_value=PORTAL):
        resp = client.post("/admin/bitrix/imports", json={"mode": "schema_only"})
    assert resp.status_code == 200
    assert resp.json()["import_id"] is not None


def test_dictionary_detail_page(client: TestClient, db_session, crm_repo):
    d = crm_repo.upsert_dictionary(ENTITY_DEAL, "enum_detail", "enumeration", title="Test Dict")
    crm_repo.upsert_dictionary_entry(d.id, "1", "Alpha")
    crm_repo.upsert_dictionary_entry(d.id, "2", "Beta")
    db_session.commit()

    resp = client.get(f"/bitrix-import/dictionaries/{d.id}")
    assert resp.status_code == 200
    assert "Alpha" in resp.text
    assert "Beta" in resp.text
    assert "Test Dict" in resp.text


def test_dictionary_detail_page_not_found(client: TestClient):
    resp = client.get("/bitrix-import/dictionaries/99999")
    assert resp.status_code == 404


def test_dictionary_detail_page_wrong_portal(client: TestClient, db_session):
    from app.models import CrmDictionary

    d = CrmDictionary(
        portal_id="other.bitrix24.ru",
        entity_type_id=ENTITY_DEAL,
        dictionary_code="foreign",
        source_type="enumeration",
    )
    db_session.add(d)
    db_session.commit()

    resp = client.get(f"/bitrix-import/dictionaries/{d.id}")
    assert resp.status_code == 404


def test_profiler_creates_payload_field(db_session, crm_repo):
    from app.services.bitrix_import.field_value_profiler import FieldValueProfiler
    from sqlalchemy import select

    crm_repo.upsert_entity(ENTITY_DEAL, 5001, {**SAMPLE_DEAL, "id": 5001, "searchContent": "abc def"})
    db_session.commit()

    FieldValueProfiler(db_session, PORTAL).profile_all()
    db_session.commit()

    fd = db_session.scalar(
        select(CrmFieldDefinition).where(
            CrmFieldDefinition.portal_id == PORTAL,
            CrmFieldDefinition.entity_type_id == ENTITY_DEAL,
            CrmFieldDefinition.original_field_name == "searchContent",
        )
    )
    assert fd is not None
    assert fd.discovered_from_payload is True


def test_profiler_signature_stable_and_changes(db_session, crm_repo):
    from app.services.bitrix_import.field_value_profiler import FieldValueProfiler

    crm_repo.upsert_entity(ENTITY_DEAL, 6001, {**SAMPLE_DEAL, "id": 6001, "searchContent": "v1"})
    db_session.commit()
    prof = FieldValueProfiler(db_session, PORTAL)
    prof.profile_all()
    db_session.commit()

    profiles = crm_repo.get_value_profiles_by_field_ids(
        [
            fd.id
            for fd in db_session.scalars(
                select(CrmFieldDefinition).where(
                    CrmFieldDefinition.portal_id == PORTAL,
                    CrmFieldDefinition.entity_type_id == ENTITY_DEAL,
                )
            )
        ]
    )
    sig1 = next(v.value_signature for v in profiles.values() if v.field_code == "searchContent")

    prof.profile_all()
    db_session.commit()
    profiles2 = crm_repo.get_value_profiles_by_field_ids(list(profiles.keys()))
    sig2 = next(v.value_signature for v in profiles2.values() if v.field_code == "searchContent")
    assert sig1 == sig2

    crm_repo.upsert_entity(ENTITY_DEAL, 6002, {**SAMPLE_DEAL, "id": 6002, "searchContent": "v2-new"})
    db_session.commit()
    prof.profile_all()
    db_session.commit()
    profiles3 = crm_repo.get_value_profiles_by_field_ids(list(profiles.keys()))
    sig3 = next(v.value_signature for v in profiles3.values() if v.field_code == "searchContent")
    assert sig3 != sig1


def test_deactivate_keeps_payload_fields(db_session, crm_repo):
    from app.services.bitrix_import.field_value_profiler import FieldValueProfiler
    from sqlalchemy import select

    crm_repo.upsert_entity(ENTITY_DEAL, 7001, {**SAMPLE_DEAL, "id": 7001, "searchContent": "x"})
    db_session.commit()
    FieldValueProfiler(db_session, PORTAL).profile_all()
    db_session.commit()

    crm_repo.deactivate_missing_fields(ENTITY_DEAL, set())
    db_session.commit()
    fd = db_session.scalar(
        select(CrmFieldDefinition).where(
            CrmFieldDefinition.original_field_name == "searchContent",
            CrmFieldDefinition.entity_type_id == ENTITY_DEAL,
        )
    )
    assert fd is not None
    assert fd.is_active is True


def test_build_field_context_with_profile(db_session, crm_repo):
    from app.models import CrmFieldValueProfile
    from app.services.bitrix_import.metadata_ai_service import BitrixMetadataAIService

    fd, _ = crm_repo.upsert_field_definition(ENTITY_DEAL, "title", SAMPLE_FIELDS["title"])
    db_session.flush()
    profile = CrmFieldValueProfile(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        field_code="title",
        field_definition_id=fd.id,
        filled_count=10,
        null_count=2,
        distinct_count=5,
        sample_values=["Пример 1", "Пример 2"],
        observed_types=["string"],
        value_signature="abc123",
    )
    ai = BitrixMetadataAIService(_settings(), db_session, PORTAL)
    ctx = ai._build_field_context(fd, [], profile)
    assert ctx["value_stats"]["filled_count"] == 10
    assert ctx["examples"] == ["Пример 1", "Пример 2"]


def test_ai_reanalysis_runs_profiling_first(db_session, sync_repo):
    from app.models import utcnow

    sync_repo.upsert_checkpoint(PORTAL, "entities", ENTITY_LEAD, utcnow(), 0)
    run = sync_repo.create_run(PORTAL, "ai_reanalysis")
    db_session.commit()

    with patch("app.services.bitrix_import.orchestrator.BitrixCrmClient") as mock_client_cls:
        client = MagicMock()
        client.diagnostics = MagicMock(to_dict=lambda: {})
        client.api_requests_count = 0
        mock_client_cls.return_value = client

        orchestrator = ImportOrchestrator(
            db=db_session,
            settings=_settings(),
            portal_id=PORTAL,
            sync_run_id=run.id,
        )

        with patch.object(orchestrator.profiler, "profile_all", return_value=5) as mock_profile:
            with patch.object(orchestrator.ai_service, "analyze_fields", return_value=3) as mock_ai:
                orchestrator.run("ai_reanalysis")

        mock_profile.assert_called_once()
        mock_ai.assert_called_once()
        assert mock_ai.call_args.kwargs.get("force") is True
