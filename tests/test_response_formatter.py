"""Tests for human-readable export plan summaries."""

from __future__ import annotations

from app.models import ENTITY_CONTACT, ENTITY_DEAL
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.intelligent_export.response_formatter import (
    build_plan_summary,
    format_assistant_message,
)


def _catalog() -> FieldCatalog:
    catalog = FieldCatalog(portal_id="test")
    for entity, code, display in (
        (ENTITY_DEAL, "TITLE", "Название"),
        (ENTITY_CONTACT, "PHONE", "Телефон"),
        (ENTITY_CONTACT, "POST", "Должность"),
        (ENTITY_CONTACT, "COMMENTS", "Описание"),
        (ENTITY_CONTACT, "LAST_NAME", "Фамилия"),
        (ENTITY_CONTACT, "NAME", "Имя"),
        (ENTITY_CONTACT, "SECOND_NAME", "Отчество"),
        (ENTITY_CONTACT, "TITLE", "Название контакта"),
    ):
        catalog.fields[(entity, code)] = FieldCatalogEntry(
            entity_type_id=entity,
            field_code=code,
            display_name=display,
            field_type="string",
            is_custom=False,
            is_multiple=code == "PHONE",
            storage="jsonb",
            sensitive=code == "PHONE",
        )
    return catalog


def _contact_plan_dict() -> dict:
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
                    {"relation_code": "deal_contact_link", "from_alias": "deal", "to_alias": "contact"}
                ],
                "filters": [
                    {
                        "field": {"entity_type_id": ENTITY_DEAL, "field_code": "TITLE", "source_alias": "deal"},
                        "op": "contains",
                        "value": "Красноярск",
                    }
                ],
                "limit": 5000,
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
                                "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "PHONE", "source_alias": "contact"},
                            },
                            "transforms": [{"op": "phone_normalize", "params": {}}],
                        },
                        {
                            "id": "full_name",
                            "header": "Имя",
                            "value": {
                                "kind": "coalesce",
                                "parts": [
                                    {
                                        "kind": "concat",
                                        "parts": [
                                            {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "LAST_NAME", "source_alias": "contact"}},
                                            {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "NAME", "source_alias": "contact"}},
                                            {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "SECOND_NAME", "source_alias": "contact"}},
                                        ],
                                        "separator": " ",
                                    },
                                    {"kind": "field", "field": {"entity_type_id": ENTITY_CONTACT, "field_code": "TITLE", "source_alias": "contact"}},
                                ],
                            },
                        },
                    ],
                }
            ],
        },
    }


def test_build_plan_summary():
    catalog = _catalog()
    plan = ExportPlan2.model_validate(_contact_plan_dict())
    summary = build_plan_summary(plan, catalog)
    assert summary["title"] == "Контакты из сделок по Красноярску"
    assert "Сделки" in summary["entities"]
    assert "Контакты" in summary["entities"]
    assert any("ФИО" in col["source"] for col in summary["columns"])
    assert summary["filters"][0]["value"] == "Красноярск"
    assert summary["limit"] == 5000


def test_format_assistant_message_replaces_echo():
    catalog = _catalog()
    plan = ExportPlan2.model_validate(_contact_plan_dict())
    summary = build_plan_summary(plan, catalog)
    msg = format_assistant_message(
        "Выгрузка контактов из сделок по Красноярску.",
        summary,
        status="validated",
    )
    assert len(msg) > 100
    assert "Колонки:" in msg
    assert "Красноярск" in msg


def test_format_assistant_message_keeps_rich_llm_text():
    catalog = _catalog()
    plan = ExportPlan2.model_validate(_contact_plan_dict())
    summary = build_plan_summary(plan, catalog)
    rich = "Подготовлена выгрузка. Колонки: телефон, ФИО, должность. Фильтр по Красноярску."
    msg = format_assistant_message(rich, summary, status="validated")
    assert rich in msg
