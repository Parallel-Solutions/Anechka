"""Unit tests for tomoru_stages helpers."""

from __future__ import annotations

import pytest

from app.models import ENTITY_DEAL
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.compiler_v2 import ExportPlanCompilerV2
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.plan_enricher import enrich_plan
from app.services.intelligent_export.tomoru_stages import (
    KpStageCatalog,
    extract_years_from_text,
    normalize_homoglyphs,
    normalize_stage_name,
    parse_stage_mentions,
    resolve_stage_id,
    try_parse_stage_code,
    try_resolve_stage_id,
)

PORTAL = "tomoru.test.bitrix24.ru"

MOCK_KP_STAGES = [
    {"id": "C15:NEW", "name": "Новая"},
    {"id": "7", "name": "КП дошло - связаться в 2020"},
    {"id": "C15:4", "name": "Тёплый"},
]


@pytest.fixture
def kp_stages() -> KpStageCatalog:
    return KpStageCatalog.from_stages(MOCK_KP_STAGES)


def test_normalize_stage_name():
    assert normalize_stage_name("  КП  дошло - связаться в 2020  ") == "кп дошло - связаться в 2020"
    assert normalize_stage_name("Тёплый") == "теплый"


def test_resolve_novaya(kp_stages):
    assert try_resolve_stage_id("Новая", kp_stages) == "C15:NEW"


def test_resolve_kp_doslo(kp_stages):
    assert try_resolve_stage_id("КП дошло - связаться в 2020", kp_stages) == "7"


def test_resolve_tepliy(kp_stages):
    assert try_resolve_stage_id("Тёплый", kp_stages) == "C15:4"
    assert try_resolve_stage_id("теплый", kp_stages) == "C15:4"


def test_resolve_stage_id_raises(kp_stages):
    with pytest.raises(ValueError):
        resolve_stage_id("Неизвестная стадия", kp_stages)


def test_parse_stage_mentions_multi(kp_stages):
    mentions = parse_stage_mentions("стадия Новая и Тёплый")
    assert mentions == ["Новая", "Тёплый"]
    resolved, unresolved = kp_stages.resolve_many(mentions)
    assert resolved == ["C15:NEW", "C15:4"]
    assert unresolved == []


def test_parse_stage_mentions_deal_number(kp_stages):
    mentions = parse_stage_mentions("туморoу, санкт-петербург, стадия сделки 7")
    assert mentions == ["7"]
    assert try_parse_stage_code("7", kp_stages) == "7"


def test_normalize_homoglyphs_tomoru():
    assert normalize_homoglyphs("туморoу") == "тумороу"


def test_extract_years_from_stage_mention():
    assert extract_years_from_text("КП дошло - связаться в 2021") == [2021]


def test_resolve_many_with_unknown(kp_stages):
    resolved, unresolved = kp_stages.resolve_many(["Новая", "Фантазия"])
    assert resolved == ["C15:NEW"]
    assert unresolved == ["Фантазия"]


def _tomoru_catalog() -> FieldCatalog:
    catalog = FieldCatalog(portal_id=PORTAL)
    for code, storage in (
        ("CLOSED", "jsonb"),
        ("CATEGORY_ID", "column"),
        ("STAGE_ID", "column"),
        ("UF_CRM_5ECE25C5D78E0", "jsonb"),
    ):
        catalog.fields[(ENTITY_DEAL, code)] = FieldCatalogEntry(
            entity_type_id=ENTITY_DEAL,
            field_code=code,
            display_name=code,
            field_type="string",
            is_custom=code.startswith("UF_"),
            is_multiple=False,
            storage=storage,
            column_name="category_id" if code == "CATEGORY_ID" else ("stage_id" if code == "STAGE_ID" else None),
            sensitive=False,
        )
    return catalog


def _tomoru_plan_with_llm_noise() -> dict:
    return {
        "schema_version": "2.0",
        "title": "Tomoru",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                "filters": [
                    {
                        "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                        "op": "eq",
                        "value": "<стадия>",
                    },
                    {
                        "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"},
                        "op": "contains",
                        "value": "КП дошло - связаться в 2020",
                    },
                ],
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [{"id": "main", "name": "Номера", "mode": "rows", "dataset_id": "deals", "columns": []}],
        },
    }


def test_enricher_spb_kp_stage(kp_stages):
    enriched = enrich_plan(
        _tomoru_plan_with_llm_noise(),
        user_message="туморoу, санкт-петербург, стадия КП дошло - связаться в 2020",
        catalog=_tomoru_catalog(),
        kp_stages=kp_stages,
    )
    filters = enriched["datasets"][0]["filters"]
    assert any(
        f.get("field", {}).get("field_code") == "STAGE_ID"
        and f.get("op") == "eq"
        and f.get("value") == "7"
        for f in filters
    )
    assert any(
        f.get("field", {}).get("field_code") == "UF_CRM_5ECE25C5D78E0" and f.get("value") == 1107 for f in filters
    )
    assert not any(
        f.get("field", {}).get("field_code") == "TITLE"
        and "кп дошло" in str(f.get("value", "")).lower()
        for f in filters
    )


def test_enricher_homoglyph_spb_stage_deal_7(kp_stages):
    plan = _tomoru_plan_with_llm_noise()
    plan["datasets"][0]["filters"].extend(
        [
            {
                "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                "op": "eq",
                "value": 7,
            },
            {
                "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"},
                "op": "contains",
                "value": "Санкт-Петербург",
            },
        ]
    )
    enriched = enrich_plan(
        plan,
        user_message="туморoу, санкт-петербург, стадия сделки 7",
        catalog=_tomoru_catalog(),
        kp_stages=kp_stages,
    )
    filters = enriched["datasets"][0]["filters"]
    assert any(
        f.get("field", {}).get("field_code") == "STAGE_ID"
        and f.get("op") == "eq"
        and f.get("value") == "7"
        for f in filters
    )
    assert any(
        f.get("field", {}).get("field_code") == "UF_CRM_5ECE25C5D78E0" and f.get("value") == 1107 for f in filters
    )
    assert not any(f.get("field", {}).get("field_code") == "TITLE" for f in filters)


def test_enricher_strips_spurious_date_from_stage_year(kp_stages):
    stages = KpStageCatalog.from_stages(
        [
            *MOCK_KP_STAGES,
            {"id": "9", "name": "КП дошло - связаться в 2021"},
        ]
    )
    plan = _tomoru_plan_with_llm_noise()
    plan["datasets"][0]["filters"].extend(
        [
            {
                "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
                "op": "gte",
                "value": "2021-01-01",
            },
            {
                "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
                "op": "lte",
                "value": "2021-12-31",
            },
        ]
    )
    enriched = enrich_plan(
        plan,
        user_message="туморoу, Москва, стадия КП дошло - связаться в 2021",
        catalog=_tomoru_catalog(),
        kp_stages=stages,
    )
    filters = enriched["datasets"][0]["filters"]
    assert not any(f.get("field", {}).get("field_code") == "DATE_CREATE" for f in filters)
    assert any(
        f.get("field", {}).get("field_code") == "STAGE_ID" and f.get("value") == "9" for f in filters
    )


def test_enricher_moscow_novaya(kp_stages):
    enriched = enrich_plan(
        _tomoru_plan_with_llm_noise(),
        user_message="Tomoru по Москве, стадия Новая",
        catalog=_tomoru_catalog(),
        kp_stages=kp_stages,
    )
    filters = enriched["datasets"][0]["filters"]
    assert any(
        f.get("field", {}).get("field_code") == "STAGE_ID"
        and f.get("op") == "eq"
        and f.get("value") == "C15:NEW"
        for f in filters
    )


def test_enricher_tomsk_tepliy(kp_stages):
    enriched = enrich_plan(
        _tomoru_plan_with_llm_noise(),
        user_message="Обзвон Tomoru — Томская область, стадия Тёплый",
        catalog=_tomoru_catalog(),
        kp_stages=kp_stages,
    )
    filters = enriched["datasets"][0]["filters"]
    assert any(
        f.get("field", {}).get("field_code") == "STAGE_ID"
        and f.get("op") == "eq"
        and f.get("value") == "C15:4"
        for f in filters
    )


def test_enricher_multi_stage_warning(kp_stages):
    warnings: list[str] = []
    enriched = enrich_plan(
        _tomoru_plan_with_llm_noise(),
        user_message="Tomoru по Москве, стадия Новая и Фантазия",
        catalog=_tomoru_catalog(),
        kp_stages=kp_stages,
        enrichment_warnings=warnings,
    )
    filters = enriched["datasets"][0]["filters"]
    assert any(f.get("field", {}).get("field_code") == "STAGE_ID" and f.get("value") == "C15:NEW" for f in filters)
    assert warnings
    assert "Фантазия" in warnings[0]


def test_resolve_stage_from_general_funnel():
    catalog = KpStageCatalog.from_stages(
        [
            {"id": "LOSE", "name": "Работы неинтерсены", "category_id": 0},
            {"id": "C15:NEW", "name": "Новая", "category_id": 15},
        ]
    )
    assert try_resolve_stage_id("Работы неинтерсены", catalog) == "LOSE"


def test_resolve_prefers_tomoru_category_for_duplicate_name():
    catalog = KpStageCatalog.from_stages(
        [
            {"id": "NEW", "name": "Новая", "category_id": 0},
            {"id": "C15:NEW", "name": "Новая", "category_id": 15},
        ]
    )
    assert try_resolve_stage_id("Новая", catalog, preferred_category_id=15) == "C15:NEW"


def test_load_merges_db_and_bitrix_api(db_session):
    from unittest.mock import MagicMock

    from app.models import CrmDictionary, CrmDictionaryEntry

    dictionary = CrmDictionary(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        dictionary_code="status_DEAL_STAGE",
        source_type="crm.status",
        is_active=True,
    )
    db_session.add(dictionary)
    db_session.flush()
    db_session.add(
        CrmDictionaryEntry(
            dictionary_id=dictionary.id,
            external_id="C15:NEW",
            raw_value="Новая",
            is_active=True,
        )
    )
    db_session.commit()

    client = MagicMock()
    client.get_categories.return_value = [{"id": 0, "name": "Общая"}]
    client.get_stages.return_value = [{"id": "LOSE", "name": "Работы неинтерсены"}]

    loaded = KpStageCatalog.load(db_session, PORTAL, bitrix_client=client)
    assert loaded.has_code("C15:NEW")
    assert loaded.has_code("LOSE")
    assert try_resolve_stage_id("Работы неинтерсены", loaded) == "LOSE"
    client.get_categories.assert_called_once()
    client.get_stages.assert_called()


def test_load_bitrix_api_failure_falls_back_to_db(db_session):
    from unittest.mock import MagicMock

    from app.models import CrmDictionary, CrmDictionaryEntry

    dictionary = CrmDictionary(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        dictionary_code="status_DEAL_STAGE",
        source_type="crm.status",
        is_active=True,
    )
    db_session.add(dictionary)
    db_session.flush()
    db_session.add(
        CrmDictionaryEntry(
            dictionary_id=dictionary.id,
            external_id="LOSE",
            raw_value="Работы неинтерсены",
            is_active=True,
        )
    )
    db_session.commit()

    client = MagicMock()
    client.get_categories.side_effect = RuntimeError("api down")

    loaded = KpStageCatalog.load(db_session, PORTAL, bitrix_client=client)
    assert try_resolve_stage_id("Работы неинтерсены", loaded) == "LOSE"


def _general_deals_plan() -> dict:
    return {
        "schema_version": "2.0",
        "title": "Сделки",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                "filters": [],
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [{"id": "main", "name": "Сделки", "mode": "rows", "dataset_id": "deals", "columns": []}],
        },
    }


def test_enricher_general_plan_stage_filter():
    stages = KpStageCatalog.from_stages([{"id": "LOSE", "name": "Работы неинтерсены", "category_id": 0}])
    catalog = FieldCatalog(portal_id=PORTAL)
    catalog.fields[(ENTITY_DEAL, "STAGE_ID")] = FieldCatalogEntry(
        entity_type_id=ENTITY_DEAL,
        field_code="STAGE_ID",
        display_name="STAGE_ID",
        field_type="string",
        is_custom=False,
        is_multiple=False,
        storage="column",
        column_name="stage_id",
        sensitive=False,
    )
    enriched = enrich_plan(
        _general_deals_plan(),
        user_message="Покажи сделки на стадии Работы неинтерсены",
        catalog=catalog,
        kp_stages=stages,
    )
    filters = enriched["datasets"][0]["filters"]
    assert any(
        f.get("field", {}).get("field_code") == "STAGE_ID"
        and f.get("op") == "eq"
        and f.get("value") == "LOSE"
        for f in filters
    )


def test_integration_enricher_compiler_deal_1955(db_session, kp_stages):
    from app.models import CrmEntity

    legacy = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=1955,
        title="КП для Метрополитена СПб",
        category_id=None,
        stage_id="7",
        payload_hash="legacy1955",
        raw_payload={"id": 1955, "stageId": "7", "closed": "N"},
    )
    db_session.add(legacy)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    for code in ("CLOSED", "CATEGORY_ID", "STAGE_ID", "UF_CRM_5ECE25C5D78E0"):
        if catalog.get(ENTITY_DEAL, code) is None:
            catalog.fields[(ENTITY_DEAL, code)] = FieldCatalogEntry(
                entity_type_id=ENTITY_DEAL,
                field_code=code,
                display_name=code,
                field_type="string",
                is_custom=code.startswith("UF_"),
                is_multiple=False,
                storage="column" if code in ("CATEGORY_ID", "STAGE_ID") else "jsonb",
                column_name={"CATEGORY_ID": "category_id", "STAGE_ID": "stage_id"}.get(code),
                sensitive=False,
            )

    enriched = enrich_plan(
        _tomoru_plan_with_llm_noise(),
        user_message="туморoу, санкт-петербург, стадия КП дошло - связаться в 2020",
        catalog=catalog,
        kp_stages=kp_stages,
    )
    plan = ExportPlan2.model_validate(enriched)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) >= 1
    rows = compiler.fetch_page(compiled, limit=10)
    assert any(r["deal"].entity_id == 1955 for r in rows)
