"""Tests for ExportPlan models, validator, and compiler."""

from __future__ import annotations

import pytest

from app.models import CrmEntity, ENTITY_DEAL
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.compiler import ExportPlanCompiler
from app.services.export_plan.models import Column, ExportPlan, FieldRef
from app.services.export_plan.validator import ExportPlanValidator, ExportScope


def _sample_plan() -> ExportPlan:
    return ExportPlan.model_validate(
        {
            "schema_version": "1.0",
            "title": "Сделки воронки 15",
            "sources": [
                {
                    "alias": "deals",
                    "entity_type_id": 2,
                    "filters": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "CATEGORY_ID"},
                            "op": "eq",
                            "value": 15,
                        }
                    ],
                    "limit": 100,
                }
            ],
            "columns": [
                {
                    "header": "ID",
                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deals"},
                },
                {
                    "header": "Название",
                    "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deals"},
                },
            ],
            "output": {"format": "xlsx", "sheets": [{"name": "Сделки"}]},
        }
    )


def _catalog_with_deal_fields() -> FieldCatalog:
    catalog = FieldCatalog(portal_id="test.bitrix24.ru")
    for code in ("ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "ASSIGNED_BY_ID"):
        catalog.fields[(2, code)] = FieldCatalogEntry(
            entity_type_id=2,
            field_code=code,
            display_name=code,
            field_type="system",
            is_custom=False,
            is_multiple=False,
            storage="column" if code != "STAGE_ID" else "column",
            column_name={
                "ID": "entity_id",
                "TITLE": "title",
                "CATEGORY_ID": "category_id",
                "STAGE_ID": "stage_id",
                "ASSIGNED_BY_ID": "assigned_by_id",
            }.get(code),
        )
    return catalog


def test_export_plan_parses_valid_json():
    plan = _sample_plan()
    assert plan.schema_version == "1.0"
    assert plan.sources[0].alias == "deals"
    assert plan.columns[0].field.source_alias == "deals"


def test_validator_accepts_valid_plan():
    plan = _sample_plan()
    validator = ExportPlanValidator(_catalog_with_deal_fields(), ExportScope(role="admin", max_rows=5000))
    result = validator.validate(plan)
    assert result.valid is True
    assert not result.issues


def test_validator_rejects_unknown_field():
    plan = _sample_plan()
    plan.columns.append(
        Column(
            header="Secret",
            field=FieldRef(entity_type_id=2, field_code="UNKNOWN_UF", source_alias="deals"),
        )
    )
    validator = ExportPlanValidator(_catalog_with_deal_fields())
    result = validator.validate(plan)
    assert result.valid is False
    assert any(i.code == "FIELD_NOT_IN_CATALOG" for i in result.issues)


def test_validator_viewer_requires_assigned_filter():
    plan = _sample_plan()
    validator = ExportPlanValidator(
        _catalog_with_deal_fields(),
        ExportScope(role="viewer", assigned_by_id=439, max_rows=5000),
    )
    result = validator.validate(plan)
    assert result.valid is False
    assert any(i.code == "SCOPE_ASSIGNED_REQUIRED" for i in result.issues)


def test_compiler_builds_query_and_fetches(db_session):
    portal_id = "test.bitrix24.ru"
    entity = CrmEntity(
        portal_id=portal_id,
        entity_type_id=ENTITY_DEAL,
        entity_id=100,
        title="Test deal",
        category_id=15,
        stage_id="C15:NEW",
        payload_hash="abc",
        raw_payload={"id": 100, "title": "Test deal"},
    )
    db_session.add(entity)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, portal_id)
    plan = _sample_plan()
    validator = ExportPlanValidator(catalog, ExportScope(role="admin", max_rows=5000))
    assert validator.validate(plan).valid

    compiler = ExportPlanCompiler(db_session, portal_id, catalog)
    compiled = compiler.compile(plan)
    assert compiled.primary_alias == "deals"
    assert compiler.count(compiled) == 1
    rows = compiler.fetch_page(compiled, limit=10)
    assert len(rows) == 1
    assert rows[0].title == "Test deal"


def test_validator_rejects_limit_above_scope():
    plan = _sample_plan()
    plan.sources[0].limit = 10000
    validator = ExportPlanValidator(_catalog_with_deal_fields(), ExportScope(max_rows=5000))
    result = validator.validate(plan)
    assert result.valid is False
    assert any(i.code in ("LIMIT_EXCEEDED", "SOURCE_LIMIT") for i in result.issues)
