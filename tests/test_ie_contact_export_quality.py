"""End-to-end quality tests for contact export from deals."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import (
    CrmContactLink,
    CrmEntity,
    CrmFieldDefinition,
    ENTITY_CONTACT,
    ENTITY_DEAL,
)
from app.routers.intelligent_export import get_planner
from app.services.auth_service import resolve_portal_id
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.dictionaries import build_dictionary_tools
from app.services.intelligent_export.plan_enricher import enrich_plan
from app.services.intelligent_export.plan_service import prepare_plan
from app.services.intelligent_export.preview_service import PreviewService
from app.services.intelligent_export.planner import FakePlanner
from app.services.intelligent_export.sheet_processor import make_sheet_processor
from app.services.intelligent_export.transform_engine import TransformContext

API = "/api/intelligent-export"
PORTAL = "test.bitrix24.ru"


def _seed_contact_field_defs(db, portal_id: str) -> None:
    for code in ("NAME", "LAST_NAME", "SECOND_NAME", "POST", "COMMENTS"):
        db.add(
            CrmFieldDefinition(
                portal_id=portal_id,
                entity_type_id=ENTITY_CONTACT,
                original_field_name=code,
                upper_name=code,
                field_type="string",
                is_active=True,
            )
        )


def _bad_contact_plan() -> dict:
    return {
        "schema_version": "2.0",
        "title": "Контакты из сделок по Красноярску",
        "datasets": [
            {
                "id": "dc",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [
                    {"alias": "deal", "entity_type_id": ENTITY_DEAL},
                    {"alias": "contact", "entity_type_id": ENTITY_CONTACT},
                ],
                "relation_refs": [
                    {"relation_code": "deal_contact", "from_alias": "deal", "to_alias": "contact"}
                ],
                "filters": [
                    {
                        "field": {"entity_type_id": ENTITY_DEAL, "field_code": "TITLE", "source_alias": "deal"},
                        "op": "contains",
                        "value": "Красноярск",
                    }
                ],
                "limit": 100,
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [
                {
                    "id": "main",
                    "name": "Контакты",
                    "mode": "rows",
                    "dataset_id": "dc",
                    "columns": [
                        {
                            "id": "phone",
                            "header": "Телефон",
                            "value": {
                                "kind": "field",
                                "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "FM", "source_alias": "contact"},
                            },
                            "transforms": [{"op": "phone_normalize", "params": {}}],
                        },
                        {
                            "id": "name",
                            "header": "Имя",
                            "value": {
                                "kind": "field",
                                "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "TITLE", "source_alias": "contact"},
                            },
                        },
                        {
                            "id": "post",
                            "header": "Должность",
                            "value": {
                                "kind": "field",
                                "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "TITLE", "source_alias": "contact"},
                            },
                        },
                    ],
                    "validation_rules": [
                        {"id": "req", "type": "required", "column_id": "phone", "severity": "error"}
                    ],
                }
            ],
        },
    }


def _seed_krasnoyarsk_scenario(db, portal_id: str) -> None:
    _seed_contact_field_defs(db, portal_id)
    db.add(
        CrmEntity(
            portal_id=portal_id,
            entity_type_id=ENTITY_DEAL,
            entity_id=100,
            title="Сделка Красноярск",
            category_id=1,
            payload_hash="d100",
            raw_payload={"id": 100, "title": "Сделка Красноярск"},
        )
    )
    db.add(
        CrmEntity(
            portal_id=portal_id,
            entity_type_id=ENTITY_CONTACT,
            entity_id=200,
            title="Отдел архитектуры",
            payload_hash="c200",
            raw_payload={
                "id": 200,
                "title": "Отдел архитектуры",
                "lastName": "Иванов",
                "name": "Иван",
                "secondName": "Иванович",
                "post": "Директор",
                "comments": "Ключевой контакт",
                "phone": [{"value": "89991234567", "valueType": "MOBILE"}],
            },
        )
    )
    db.add(
        CrmContactLink(
            portal_id=portal_id,
            contact_id=200,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=100,
            is_primary=True,
        )
    )


def test_enricher_and_preview_krasnoyarsk_contacts(db_session):
    portal_id = resolve_portal_id(get_settings())
    _seed_krasnoyarsk_scenario(db_session, portal_id)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, portal_id)
    enriched = enrich_plan(
        _bad_contact_plan(),
        user_message="выгрузи телефоны, имена, должности из сделок по красноярску",
        catalog=catalog,
    )
    prepared = prepare_plan(db_session, portal_id, ExportScope(role="admin", allow_sensitive_fields=True), enriched)
    assert prepared.valid, prepared.validation.issues

    plan = prepared.plan
    assert plan is not None
    ref = plan.datasets[0].relation_refs[0].relation_code
    assert ref == "deal_contact_link"

    scope = ExportScope(role="admin", allow_sensitive_fields=True)
    resolve_label, dict_check = build_dictionary_tools(db_session, portal_id)
    processor = make_sheet_processor(TransformContext(resolve_dictionary=resolve_label), dict_check)
    preview = PreviewService(db_session, get_settings(), portal_id, scope, catalog, sheet_processor=processor)
    result = preview.preview(plan)
    main = next(s for s in result["sheets"] if s.get("mode") != "errors")
    row = main["rows"][0]
    assert row["name"] == "Иванов Иван Иванович"
    assert row["name"] != "Отдел архитектуры"
    assert "791991234567" in str(row["phone"]).replace(" ", "").replace("-", "").replace("+", "") or row["phone"]
    error_sheets = [s for s in result["sheets"] if s.get("mode") == "errors"]
    assert not error_sheets or error_sheets[0]["total_count"] == 0


@pytest.fixture()
def use_fake_planner(client):
    holder = {}

    def _install(responses):
        fake = FakePlanner(responses=responses)
        holder["fake"] = fake
        client.app.dependency_overrides[get_planner] = lambda: fake
        return fake

    yield _install
    client.app.dependency_overrides.pop(get_planner, None)


def test_chat_returns_plan_summary_for_contacts(client, db_session, use_fake_planner):
    portal_id = resolve_portal_id(get_settings())
    _seed_krasnoyarsk_scenario(db_session, portal_id)
    db_session.commit()

    use_fake_planner(
        [
            {
                "status": "candidate_ready",
                "assistant_message": "Выгрузка контактов из сделок по Красноярску.",
                "plan": _bad_contact_plan(),
            }
        ]
    )
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    resp = client.post(
        f"{API}/conversations/{cid}/chat",
        json={"message": "выгрузи телефоны, имена, должности из сделок по красноярску"},
    ).json()
    assert resp["status"] == "validated"
    assert resp["plan_summary"] is not None
    assert resp["plan_summary"]["columns"]
    assert len(resp["assistant_message"]) > 100
    assert "Колонки:" in resp["assistant_message"] or "колонк" in resp["assistant_message"].lower()

    msgs = client.get(f"{API}/conversations/{cid}/messages").json()["messages"]
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["metadata"].get("plan_summary") is not None
