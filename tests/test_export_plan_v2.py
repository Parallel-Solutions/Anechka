"""Tests for ExportPlan 2.0: models, structural validator, registries, adapter."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.export_plan.adapter import adapt_v1_to_v2
from app.services.export_plan.models import ExportPlan as ExportPlanV1
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.export_plan.plan_normalizer import normalize_llm_plan
from app.services.export_plan.registry import (
    get_relation,
    validate_rule_params,
    validate_transform_params,
)
from app.services.export_plan.validator_v2 import validate_structure


def _multi_sheet_plan() -> dict:
    return {
        "schema_version": "2.0",
        "title": "Импорт в другую CRM",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": 2,
                "sources": [{"alias": "deal", "entity_type_id": 2}],
                "filters": [
                    {"field": {"entity_type_id": 2, "field_code": "CATEGORY_ID", "source_alias": "deal"}, "op": "eq", "value": 1}
                ],
                "limit": 5000,
            },
            {
                "id": "leads",
                "primary_entity_type_id": 1,
                "sources": [{"alias": "lead", "entity_type_id": 1}],
                "limit": 5000,
            },
        ],
        "workbook": {
            "format": "xlsx",
            "filename_label": "crm_export",
            "include_errors_sheet": True,
            "sheets": [
                {
                    "id": "deals_sheet",
                    "name": "Сделки",
                    "mode": "rows",
                    "dataset_id": "deals",
                    "columns": [
                        {
                            "id": "phone",
                            "header": "Телефон",
                            "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "PHONE", "source_alias": "deal"}},
                            "transforms": [{"op": "phone_digits_only", "params": {}}],
                            "excel_format": "@",
                        },
                        {
                            "id": "amount",
                            "header": "Сумма",
                            "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "OPPORTUNITY", "source_alias": "deal"}},
                        },
                    ],
                    "validation_rules": [
                        {"id": "amount_required", "type": "required", "column_id": "amount", "severity": "error"}
                    ],
                    "error_policy": "route_to_errors",
                },
                {
                    "id": "leads_sheet",
                    "name": "Лиды",
                    "mode": "rows",
                    "dataset_id": "leads",
                    "columns": [
                        {
                            "id": "title",
                            "header": "Название",
                            "value": {"kind": "field", "field": {"entity_type_id": 1, "field_code": "TITLE", "source_alias": "lead"}},
                        }
                    ],
                },
            ],
        },
    }


def test_v2_plan_parses_and_fills_aliases():
    plan = ExportPlan2.model_validate(_multi_sheet_plan())
    assert plan.schema_version == "2.0"
    assert len(plan.datasets) == 2
    assert plan.workbook.sheets[0].columns[0].value.field.source_alias == "deal"


def test_v2_structure_valid():
    plan = ExportPlan2.model_validate(_multi_sheet_plan())
    result = validate_structure(plan)
    assert result.valid, result.issues


def test_v2_rejects_extra_properties():
    data = _multi_sheet_plan()
    data["unexpected"] = 1
    with pytest.raises(ValidationError):
        ExportPlan2.model_validate(data)


def test_v2_sheet_requires_dataset():
    data = _multi_sheet_plan()
    data["workbook"]["sheets"][0]["dataset_id"] = None
    with pytest.raises(ValidationError):
        ExportPlan2.model_validate(data)


def test_structure_rejects_unknown_dataset_reference():
    data = _multi_sheet_plan()
    data["workbook"]["sheets"][0]["dataset_id"] = "ghost"
    plan = ExportPlan2.model_validate(data)
    result = validate_structure(plan)
    assert not result.valid
    assert any(i.code == "DATASET_NOT_FOUND" for i in result.issues)


def test_structure_rejects_unknown_relation():
    data = _multi_sheet_plan()
    data["datasets"][0]["sources"].append({"alias": "contact", "entity_type_id": 3})
    data["datasets"][0]["relation_refs"] = [
        {"relation_code": "made_up_relation", "from_alias": "deal", "to_alias": "contact"}
    ]
    plan = ExportPlan2.model_validate(data)
    result = validate_structure(plan)
    assert not result.valid
    assert any(i.code == "RELATION_NOT_ALLOWED" for i in result.issues)


def test_structure_accepts_known_relation():
    data = _multi_sheet_plan()
    data["datasets"][0]["sources"].append({"alias": "contact", "entity_type_id": 3})
    data["datasets"][0]["relation_refs"] = [
        {"relation_code": "deal_contact", "from_alias": "deal", "to_alias": "contact"}
    ]
    plan = ExportPlan2.model_validate(data)
    result = validate_structure(plan)
    assert result.valid, result.issues


def test_transform_params_validation():
    parsed, err = validate_transform_params("phone_normalize", {"country": "RU"})
    assert err is None and parsed is not None
    _, err2 = validate_transform_params("phone_normalize", {"country": "RUS"})
    assert err2 is not None
    _, err3 = validate_transform_params("totally_unknown", {})
    assert err3 is not None


def test_mapping_lookup_unknown_policy():
    _, err = validate_transform_params("mapping_lookup", {"mapping": {"NEW": "new"}, "on_unknown": "bogus"})
    assert err is not None


def test_regex_rule_rejects_catastrophic_pattern():
    _, err = validate_rule_params("regex", {"pattern": "(.*)+"})
    assert err is not None
    parsed, ok = validate_rule_params("regex", {"pattern": r"^\d{10}$"})
    assert ok is None and parsed is not None


def test_relation_registry_lookup():
    assert get_relation("deal_contact") is not None
    assert get_relation("nope") is None


def test_v1_to_v2_adapter():
    v1 = ExportPlanV1.model_validate(
        {
            "schema_version": "1.0",
            "title": "Старый план",
            "sources": [
                {
                    "alias": "deals",
                    "entity_type_id": 2,
                    "filters": [
                        {"field": {"entity_type_id": 2, "field_code": "CATEGORY_ID"}, "op": "eq", "value": 15}
                    ],
                    "limit": 100,
                }
            ],
            "transforms": [{"id": "norm", "op": "phone_normalize", "params": {}}],
            "columns": [
                {"header": "ID", "field": {"entity_type_id": 2, "field_code": "ID"}},
                {"header": "Телефон", "field": {"entity_type_id": 2, "field_code": "PHONE"}, "transform_id": "norm"},
            ],
            "output": {"format": "xlsx", "sheets": [{"name": "Сделки"}]},
        }
    )
    v2 = adapt_v1_to_v2(v1)
    assert v2.schema_version == "2.0"
    assert len(v2.datasets) == 1
    assert v2.datasets[0].id == "main"
    assert len(v2.workbook.sheets) == 1
    sheet = v2.workbook.sheets[0]
    assert sheet.name == "Сделки"
    assert len(sheet.columns) == 2
    phone_col = next(c for c in sheet.columns if c.header == "Телефон")
    assert phone_col.transforms and phone_col.transforms[0].op == "phone_normalize"
    # adapter output must pass structural validation
    result = validate_structure(v2)
    assert result.valid, result.issues


# --- plan_normalizer ---------------------------------------------------------


def test_normalize_sort_op_to_direction():
    plan = {
        "schema_version": "2.0",
        "datasets": [
            {
                "id": "deals",
                "sort": [
                    {
                        "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
                        "op": "desc",
                    }
                ],
            }
        ],
    }
    normalized = normalize_llm_plan(plan)
    sort_item = normalized["datasets"][0]["sort"][0]
    assert "op" not in sort_item
    assert sort_item["direction"] == "desc"


def test_normalize_sort_direction_case_and_numeric():
    plan = {
        "datasets": [
            {"id": "d", "sort": [{"field": {"entity_type_id": 2, "field_code": "ID"}, "order": "DESC"}]},
        ],
    }
    normalized = normalize_llm_plan(plan)
    assert normalized["datasets"][0]["sort"][0]["direction"] == "desc"

    plan2 = {"datasets": [{"id": "d", "sort": [{"field": {"entity_type_id": 2, "field_code": "ID"}, "dir": -1}]}]}
    assert normalize_llm_plan(plan2)["datasets"][0]["sort"][0]["direction"] == "desc"


def test_normalize_filter_op_synonyms():
    plan = {
        "datasets": [
            {
                "id": "d",
                "filters": [
                    {"field": {"entity_type_id": 2, "field_code": "ID"}, "op": "equals", "value": 1},
                    {"field": {"entity_type_id": 2, "field_code": "TITLE"}, "op": ">", "value": "a"},
                ],
            }
        ],
    }
    normalized = normalize_llm_plan(plan)
    filters = normalized["datasets"][0]["filters"]
    assert filters[0]["op"] == "eq"
    assert filters[1]["op"] == "gt"


def test_normalize_schema_version():
    assert normalize_llm_plan({"schema_version": 2})["schema_version"] == "2.0"
    assert normalize_llm_plan({"schema_version": "2"})["schema_version"] == "2.0"


def test_normalize_idempotent_and_preserves_unknown_keys():
    plan = {
        "schema_version": "2.0",
        "extra_top": "keep",
        "datasets": [
            {
                "id": "d",
                "sort": [{"field": {"entity_type_id": 2, "field_code": "ID"}, "op": "desc", "custom": True}],
            }
        ],
    }
    once = normalize_llm_plan(plan)
    twice = normalize_llm_plan(once)
    assert once == twice
    assert once["extra_top"] == "keep"
    assert once["datasets"][0]["sort"][0]["custom"] is True


def test_normalize_unknown_direction_preserved():
    plan = {
        "datasets": [
            {
                "id": "d",
                "sort": [
                    {
                        "field": {"entity_type_id": 2, "field_code": "ID"},
                        "op": "downward",
                    }
                ],
            }
        ],
    }
    normalized = normalize_llm_plan(plan)
    assert normalized["datasets"][0]["sort"][0]["direction"] == "downward"


def test_unknown_direction_fails_pydantic():
    plan = normalize_llm_plan(
        {
            "schema_version": "2.0",
            "title": "Test",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "sort": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
                            "direction": "downward",
                        }
                    ],
                }
            ],
            "workbook": {
                "format": "xlsx",
                "sheets": [{"id": "main", "name": "Data", "mode": "rows", "dataset_id": "deals", "columns": []}],
            },
        }
    )
    with pytest.raises(ValidationError):
        ExportPlan2.model_validate(plan)

