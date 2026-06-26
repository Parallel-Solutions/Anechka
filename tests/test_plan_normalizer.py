"""Tests for normalize_llm_plan region filter sanitization."""

from __future__ import annotations

from app.services.export_plan.plan_normalizer import normalize_llm_plan
from app.services.intelligent_export.contact_phone_heuristic import TOMORU_REGION_FIELD
from app.services.intelligent_export.tomoru_regions import ORENBURG_REGION_SENTINEL

_REGION_FIELD = {
    "entity_type_id": 2,
    "field_code": TOMORU_REGION_FIELD,
    "source_alias": "deal",
}


def _dataset_with_region_filters(*filters: dict) -> dict:
    return {
        "schema_version": "2.0",
        "title": "Test",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": 2,
                "sources": [{"alias": "deal", "entity_type_id": 2}],
                "filters": list(filters),
                "limit": 100,
            }
        ],
        "workbook": {"format": "xlsx", "sheets": []},
    }


def _region_filter(value) -> dict:
    return {"field": _REGION_FIELD, "op": "eq", "value": value}


def test_sanitize_drops_placeholder_when_valid_duplicate_exists():
    plan = _dataset_with_region_filters(
        _region_filter("<ID региона Санкт-Петербурга>"),
        _region_filter(1107),
    )
    normalized = normalize_llm_plan(plan)
    region_filters = [
        f
        for f in normalized["datasets"][0]["filters"]
        if f.get("field", {}).get("field_code") == TOMORU_REGION_FIELD
    ]
    assert len(region_filters) == 1
    assert region_filters[0]["value"] == 1107


def test_sanitize_resolves_lone_placeholder_to_region_id():
    plan = _dataset_with_region_filters(_region_filter("<ID региона Санкт-Петербурга>"))
    normalized = normalize_llm_plan(plan)
    region_filters = [
        f
        for f in normalized["datasets"][0]["filters"]
        if f.get("field", {}).get("field_code") == TOMORU_REGION_FIELD
    ]
    assert len(region_filters) == 1
    assert region_filters[0]["value"] == 1107


def test_sanitize_preserves_orenburg_sentinel():
    plan = _dataset_with_region_filters(_region_filter(ORENBURG_REGION_SENTINEL))
    normalized = normalize_llm_plan(plan)
    region_filters = [
        f
        for f in normalized["datasets"][0]["filters"]
        if f.get("field", {}).get("field_code") == TOMORU_REGION_FIELD
    ]
    assert len(region_filters) == 1
    assert region_filters[0]["value"] == ORENBURG_REGION_SENTINEL


def test_sanitize_idempotent():
    plan = _dataset_with_region_filters(
        _region_filter("<ID региона Санкт-Петербурга>"),
        _region_filter(1107),
    )
    once = normalize_llm_plan(plan)
    twice = normalize_llm_plan(once)
    assert once == twice
