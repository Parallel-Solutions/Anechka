"""Phase E: date tokens, planner orchestration (fake planner, no network)."""

from __future__ import annotations

from datetime import date

from app.config import get_settings
from app.services.auth_service import resolve_portal_id
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.validator import ExportScope, ValidationResult
from app.services.intelligent_export.date_tokens import resolve_date_tokens, resolve_token
from app.services.intelligent_export.plan_service import enrich_issues, format_fix_suggestions
from app.services.intelligent_export.planner import FakePlanner, plan_turn
from app.services.intelligent_export.service import (
    IntelligentExportService,
    _extract_field_codes,
    _tokens,
)

TODAY = date(2026, 6, 25)


def test_resolve_token_variants():
    assert resolve_token("@today", TODAY) == "2026-06-25"
    assert resolve_token("@today-30d", TODAY) == "2026-05-26"
    assert resolve_token("@month_start", TODAY) == "2026-06-01"
    assert resolve_token("@month_end", TODAY) == "2026-06-30"
    assert resolve_token("@prev_month_start", TODAY) == "2026-05-01"
    assert resolve_token("@prev_month_end", TODAY) == "2026-05-31"
    assert resolve_token("@year_start", TODAY) == "2026-01-01"
    assert resolve_token("not a token", TODAY) is None


def test_resolve_date_tokens_in_plan():
    plan = {"datasets": [{"filters": [{"op": "gte", "value": "@today-7d"}]}]}
    resolved = resolve_date_tokens(plan, TODAY)
    assert resolved["datasets"][0]["filters"][0]["value"] == "2026-06-18"


def _valid_plan():
    return {
        "schema_version": "2.0",
        "title": "Сделки",
        "datasets": [
            {"id": "deals", "primary_entity_type_id": 2, "sources": [{"alias": "deal", "entity_type_id": 2}]}
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [
                {
                    "id": "s",
                    "name": "Сделки",
                    "mode": "rows",
                    "dataset_id": "deals",
                    "columns": [
                        {"id": "idc", "header": "ID", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"}}}
                    ],
                }
            ],
        },
    }


def _invalid_plan():
    plan = _valid_plan()
    plan["workbook"]["sheets"][0]["columns"].append(
        {"id": "bad", "header": "X", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "NO_FIELD", "source_alias": "deal"}}}
    )
    return plan


def _ctx():
    return {"today": "2026-06-25"}


def _scope():
    return ExportScope(role="admin", allow_sensitive_fields=True)


def _portal():
    return resolve_portal_id(get_settings())


def test_plan_turn_needs_clarification(db_session):
    planner = FakePlanner(responses=[{"status": "needs_clarification", "assistant_message": "Какой период?", "clarifying_questions": ["Период?"]}])
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="выгрузи сделки")
    assert result.response.status == "needs_clarification"
    assert result.prepared is None


def test_plan_turn_validated(db_session):
    planner = FakePlanner(responses=[{"status": "candidate_ready", "assistant_message": "Готово", "plan": _valid_plan()}])
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="сделки")
    assert result.response.status == "validated"
    assert result.prepared is not None and result.prepared.valid


def _plan_with_sort_op_instead_of_direction():
    plan = _valid_plan()
    plan["datasets"][0]["sort"] = [
        {
            "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
            "op": "desc",
        }
    ]
    return plan


def test_plan_turn_validates_plan_with_sort_op_normalized(db_session):
    planner = FakePlanner(
        responses=[
            {
                "status": "candidate_ready",
                "assistant_message": "Готово",
                "plan": _plan_with_sort_op_instead_of_direction(),
            }
        ]
    )
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="сделки")
    assert result.response.status == "validated"
    assert result.prepared is not None and result.prepared.valid
    assert result.response.plan["datasets"][0]["sort"][0]["direction"] == "desc"
    assert "op" not in result.response.plan["datasets"][0]["sort"][0]


def test_plan_turn_rejected_on_invalid_direction(db_session):
    plan = _valid_plan()
    plan["datasets"][0]["sort"] = [
        {
            "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
            "direction": "downward",
        }
    ]
    planner = FakePlanner(
        responses=[{"status": "candidate_ready", "assistant_message": "x", "plan": plan}],
        supports_repair=False,
    )
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="сделки")
    assert result.response.status == "rejected"
    assert result.prepared is not None and not result.prepared.valid
    issues = result.prepared.validation.issues
    assert any(i.code == "SCHEMA_INVALID" and "sort" in i.path and "direction" in i.path for i in issues)


def test_plan_turn_rejected_when_invalid(db_session):
    planner = FakePlanner(responses=[{"status": "candidate_ready", "assistant_message": "x", "plan": _invalid_plan()}], supports_repair=False)
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="сделки")
    assert result.response.status == "rejected"
    assert not result.prepared.valid


def test_plan_turn_repairs_invalid_then_valid(db_session):
    planner = FakePlanner(
        responses=[
            {"status": "candidate_ready", "assistant_message": "v1", "plan": _invalid_plan()},
            {"status": "candidate_ready", "assistant_message": "v2", "plan": _valid_plan()},
        ],
        supports_repair=True,
    )
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="сделки")
    assert result.response.status == "validated"
    assert len(planner.calls) == 2
    assert planner.calls[1]["prior_errors"]  # repair call received the errors


def test_plan_turn_multi_repair_until_valid(db_session):
    planner = FakePlanner(
        responses=[
            {"status": "candidate_ready", "assistant_message": "v1", "plan": _invalid_plan()},
            {"status": "candidate_ready", "assistant_message": "v2", "plan": _invalid_plan()},
            {"status": "candidate_ready", "assistant_message": "v3", "plan": _valid_plan()},
        ],
        supports_repair=True,
    )
    result = plan_turn(
        planner,
        db=db_session,
        portal_id=_portal(),
        scope=_scope(),
        context=_ctx(),
        message="сделки",
        max_repair_attempts=2,
    )
    assert result.response.status == "validated"
    assert len(planner.calls) == 3
    # repair calls receive enriched validation errors with hints
    assert planner.calls[1]["prior_errors"]
    assert any("hint" in e for e in planner.calls[1]["prior_errors"])


def test_plan_turn_rejected_after_repair_budget(db_session):
    planner = FakePlanner(
        responses=[{"status": "candidate_ready", "assistant_message": "x", "plan": _invalid_plan()}],
        supports_repair=True,
    )
    result = plan_turn(
        planner,
        db=db_session,
        portal_id=_portal(),
        scope=_scope(),
        context=_ctx(),
        message="сделки",
        max_repair_attempts=2,
    )
    assert result.response.status == "rejected"
    assert len(planner.calls) == 3  # initial + 2 repair attempts


def test_plan_turn_rejected_after_seven_repairs(db_session):
    planner = FakePlanner(
        responses=[{"status": "candidate_ready", "assistant_message": "x", "plan": _invalid_plan()}],
        supports_repair=True,
    )
    result = plan_turn(
        planner,
        db=db_session,
        portal_id=_portal(),
        scope=_scope(),
        context=_ctx(),
        message="сделки",
        max_repair_attempts=7,
    )
    assert result.response.status == "rejected"
    assert len(planner.calls) == 8  # initial + 7 repair attempts


def test_enrich_issues_field_not_allowed_suggests(db_session):
    catalog = FieldCatalog.load(db_session, _portal())
    result = ValidationResult(valid=True)
    result.add("FIELD_NOT_ALLOWED", "Field TIT not in catalog for entity_type_id=2", "p")
    enriched = enrich_issues(result, catalog)
    assert enriched[0]["hint"]
    codes = {s["field_code"] for s in enriched[0].get("suggestions", [])}
    assert "TITLE" in codes


def test_enrich_issues_filter_op_lists_allowed(db_session):
    catalog = FieldCatalog.load(db_session, _portal())
    result = ValidationResult(valid=True)
    result.add("FILTER_OP_NOT_ALLOWED", "op contains not allowed for OPPORTUNITY (number)", "p")
    enriched = enrich_issues(result, catalog)
    assert "contains" not in enriched[0]["allowed_filter_ops"]
    assert "gte" in enriched[0]["allowed_filter_ops"]


def test_enrich_issues_schema_invalid_sort_op_hint(db_session):
    catalog = FieldCatalog.load(db_session, _portal())
    result = ValidationResult(valid=True)
    result.add("SCHEMA_INVALID", "Extra inputs are not permitted", "datasets.0.sort.0.op")
    enriched = enrich_issues(result, catalog)
    assert "direction" in enriched[0]["hint"]
    assert "op" in enriched[0]["hint"]
    assert enriched[0]["message"] == "Extra inputs are not permitted"


def test_format_fix_suggestions_field_not_allowed(db_session):
    catalog = FieldCatalog.load(db_session, _portal())
    result = ValidationResult(valid=True)
    result.add("FIELD_NOT_ALLOWED", "Field TIT not in catalog for entity_type_id=2", "p")
    suggestions = format_fix_suggestions(catalog, result)
    assert len(suggestions) == 1
    assert "TIT" in suggestions[0]
    assert "TITLE" in suggestions[0]


def test_format_fix_suggestions_filter_op(db_session):
    catalog = FieldCatalog.load(db_session, _portal())
    result = ValidationResult(valid=True)
    result.add("FILTER_OP_NOT_ALLOWED", "op contains not allowed for OPPORTUNITY (number)", "p")
    suggestions = format_fix_suggestions(catalog, result)
    assert len(suggestions) == 1
    assert "OPPORTUNITY" in suggestions[0]
    assert "gte" in suggestions[0]


def _custom_catalog() -> FieldCatalog:
    cat = FieldCatalog(portal_id="default")

    def add(code: str, display: str) -> None:
        cat.fields[(2, code)] = FieldCatalogEntry(
            entity_type_id=2,
            field_code=code,
            display_name=display,
            field_type="string",
            is_custom=True,
            is_multiple=False,
            storage="jsonb",
            data_type="string",
        )

    for code in ("TITLE", "STAGE_ID", "OPPORTUNITY", "DATE_CREATE"):
        add(code, code)
    for i in range(30):
        add(f"UF_X{i}", f"Прочее {i}")
    add("UF_BUDGET_NOTE", "Бюджет проекта")
    return cat


def _svc(budget: int) -> IntelligentExportService:
    settings = get_settings().model_copy(update={"ie_catalog_field_budget": budget})
    return IntelligentExportService(db=None, settings=settings, portal_id="default")


def test_select_catalog_returns_all_when_fits():
    cat = _custom_catalog()
    out = _svc(500)._select_catalog(cat, _scope(), "бюджет", None, [])
    assert len(out) == len(cat.fields)


def test_select_catalog_budget_keeps_core_and_relevant():
    cat = _custom_catalog()
    out = _svc(8)._select_catalog(cat, _scope(), "покажи бюджет проекта", None, [])
    codes = {c["field_code"] for c in out}
    assert len(out) <= 8
    assert {"TITLE", "STAGE_ID", "OPPORTUNITY", "DATE_CREATE"} <= codes
    assert "UF_BUDGET_NOTE" in codes


def test_extract_field_codes_walks_plan():
    codes = _extract_field_codes(_valid_plan())
    assert "ID" in codes


def test_tokens_dedup_and_min_length():
    toks = _tokens("Сделки за ИЮНЬ за", "июнь")
    assert "сделки" in toks
    assert "за" not in toks
    assert toks.count("июнь") == 1


def test_plan_turn_accepts_null_list_fields_from_llm(db_session):
    planner = FakePlanner(
        responses=[
            {
                "status": "candidate_ready",
                "assistant_message": "Готово",
                "plan": _valid_plan(),
                "clarifying_questions": None,
                "proposed_memory": None,
                "used_memory_ids": None,
            }
        ]
    )
    result = plan_turn(planner, db=db_session, portal_id=_portal(), scope=_scope(), context=_ctx(), message="сделки")
    assert result.response.status == "validated"
    assert result.prepared is not None and result.prepared.valid
