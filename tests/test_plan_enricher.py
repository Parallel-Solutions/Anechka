"""Tests for deterministic plan enrichment."""

from __future__ import annotations

from app.models import ENTITY_CONTACT, ENTITY_DEAL
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.intelligent_export.plan_enricher import enrich_plan

PORTAL = "test.bitrix24.ru"


def _contact_catalog() -> FieldCatalog:
    catalog = FieldCatalog(portal_id=PORTAL)
    for code in ("NAME", "LAST_NAME", "SECOND_NAME", "TITLE", "POST", "COMMENTS", "PHONE", "FM"):
        catalog.fields[(ENTITY_CONTACT, code)] = FieldCatalogEntry(
            entity_type_id=ENTITY_CONTACT,
            field_code=code,
            display_name=code,
            field_type="string",
            is_custom=False,
            is_multiple=code in ("PHONE", "FM"),
            storage="jsonb",
            sensitive=code in ("PHONE", "FM"),
        )
    return catalog


def _contact_column(header: str, field_code: str, col_id: str = "col") -> dict:
    return {
        "id": col_id,
        "header": header,
        "value": {
            "kind": "field",
            "field": {
                "entity_type_id": ENTITY_CONTACT,
                "field_code": field_code,
                "source_alias": "contact",
            },
        },
    }


def _base_plan(*, columns: list[dict], relation_code: str = "deal_contact_link") -> dict:
    return {
        "schema_version": "2.0",
        "title": "Test",
        "datasets": [
            {
                "id": "dc",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [
                    {"alias": "deal", "entity_type_id": ENTITY_DEAL},
                    {"alias": "contact", "entity_type_id": ENTITY_CONTACT},
                ],
                "relation_refs": [
                    {"relation_code": relation_code, "from_alias": "deal", "to_alias": "contact"}
                ],
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
                    "columns": columns,
                }
            ],
        },
    }


def test_enricher_title_to_fio():
    catalog = _contact_catalog()
    plan = _base_plan(columns=[_contact_column("Имя", "TITLE", "name")])
    enriched = enrich_plan(plan, user_message="выгрузи имена", catalog=catalog)
    value = enriched["workbook"]["sheets"][0]["columns"][0]["value"]
    assert value["kind"] == "coalesce"
    assert value["parts"][0]["kind"] == "concat"
    part_codes = [p["field"]["field_code"] for p in value["parts"][0]["parts"]]
    assert part_codes == ["LAST_NAME", "NAME", "SECOND_NAME"]


def test_enricher_fm_to_phone_with_normalize():
    catalog = _contact_catalog()
    plan = _base_plan(columns=[_contact_column("Телефон", "FM", "phone")])
    enriched = enrich_plan(plan, user_message="телефоны", catalog=catalog)
    col = enriched["workbook"]["sheets"][0]["columns"][0]
    assert col["value"]["field"]["field_code"] == "PHONE"
    assert any(t["op"] == "phone_normalize" for t in col.get("transforms", []))


def test_enricher_deal_contact_to_link():
    catalog = _contact_catalog()
    plan = _base_plan(columns=[_contact_column("Контакт", "TITLE")], relation_code="deal_contact")
    enriched = enrich_plan(plan, user_message="контакты сделок", catalog=catalog)
    ref = enriched["datasets"][0]["relation_refs"][0]
    assert ref["relation_code"] == "deal_contact_link"


def test_enricher_strips_required_on_phone_by_default():
    catalog = _contact_catalog()
    plan = _base_plan(columns=[_contact_column("Телефон", "PHONE", "phone")])
    plan["workbook"]["sheets"][0]["validation_rules"] = [
        {"id": "req", "type": "required", "column_id": "phone", "severity": "error"}
    ]
    enriched = enrich_plan(plan, user_message="телефоны", catalog=catalog)
    assert enriched["workbook"]["sheets"][0]["validation_rules"] == []


def test_enricher_keeps_required_when_user_asks_strict():
    catalog = _contact_catalog()
    plan = _base_plan(columns=[_contact_column("Телефон", "PHONE", "phone")])
    plan["workbook"]["sheets"][0]["validation_rules"] = [
        {"id": "req", "type": "required", "column_id": "phone", "severity": "error"}
    ]
    enriched = enrich_plan(plan, user_message="только с телефоном", catalog=catalog)
    assert len(enriched["workbook"]["sheets"][0]["validation_rules"]) == 1


def test_sanitize_tomoru_plan_strips_spurious_date_create_range():
    from app.services.intelligent_export.plan_enricher import sanitize_tomoru_plan

    plan = {
        "schema_version": "2.0",
        "title": "Сделки для обзвона в Москве 2021",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                "filters": [
                    {
                        "field": {"entity_type_id": ENTITY_DEAL, "field_code": "CLOSED", "source_alias": "deal"},
                        "op": "eq",
                        "value": "N",
                    },
                    {
                        "field": {"entity_type_id": ENTITY_DEAL, "field_code": "STAGE_ID", "source_alias": "deal"},
                        "op": "eq",
                        "value": "9",
                    },
                    {
                        "field": {"entity_type_id": ENTITY_DEAL, "field_code": "DATE_CREATE", "source_alias": "deal"},
                        "op": "gte",
                        "value": "2021-01-01",
                    },
                    {
                        "field": {"entity_type_id": ENTITY_DEAL, "field_code": "DATE_CREATE", "source_alias": "deal"},
                        "op": "lte",
                        "value": "2021-12-31",
                    },
                ],
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [
                {
                    "id": "main",
                    "name": "Номера",
                    "mode": "rows",
                    "dataset_id": "deals",
                    "columns": [],
                    "post_process": {"op": "tomoru_phones", "deal_alias": "deal"},
                }
            ],
        },
    }
    cleaned = sanitize_tomoru_plan(plan)
    filters = cleaned["datasets"][0]["filters"]
    assert not any((f.get("field") or {}).get("field_code") == "DATE_CREATE" for f in filters)
    assert any((f.get("field") or {}).get("field_code") == "STAGE_ID" for f in filters)
