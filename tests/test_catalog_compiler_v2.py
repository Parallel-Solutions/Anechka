"""Phase D: catalog descriptors/search, v2 compiler joins/scope, catalog validator."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import CrmContactLink, CrmEntity, ENTITY_COMPANY, ENTITY_CONTACT, ENTITY_DEAL
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.catalog_validator import CatalogScopeValidator
from app.services.export_plan.compiler_v2 import CompileError, ExportPlanCompilerV2
from app.services.export_plan.models_v2 import Dataset, ExportPlan2
from app.services.export_plan.validator import ExportScope

PORTAL = "test.bitrix24.ru"


def _seed_deal(db, entity_id, *, category_id=1, contact_id=None, amount=None, phone=None, assigned=None, stage_id=None, created_time=None):
    payload = {"id": entity_id, "title": f"Deal {entity_id}"}
    if contact_id is not None:
        payload["contactId"] = contact_id
    if phone is not None:
        payload["phone"] = [{"value": phone, "valueType": "WORK"}]
    ent = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=entity_id,
        title=f"Deal {entity_id}",
        category_id=category_id,
        stage_id=stage_id,
        amount=amount,
        assigned_by_id=assigned,
        created_time=created_time,
        payload_hash=f"h{entity_id}",
        raw_payload=payload,
    )
    db.add(ent)
    return ent


def _seed_contact(db, entity_id, *, title="Contact", phone=None):
    payload = {"id": entity_id, "title": title}
    if phone is not None:
        payload["phone"] = [{"value": phone, "valueType": "WORK"}]
    ent = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_CONTACT,
        entity_id=entity_id,
        title=title,
        payload_hash=f"c{entity_id}",
        raw_payload=payload,
    )
    db.add(ent)
    return ent


def _seed_link(db, *, contact_id, parent_id, parent_type=ENTITY_DEAL, is_primary=False):
    link = CrmContactLink(
        portal_id=PORTAL,
        contact_id=contact_id,
        parent_entity_type_id=parent_type,
        parent_entity_id=parent_id,
        is_primary=is_primary,
    )
    db.add(link)
    return link


def _link_dataset(relation_code):
    return Dataset.model_validate(
        {
            "id": "d",
            "primary_entity_type_id": 2,
            "sources": [
                {"alias": "deal", "entity_type_id": 2},
                {"alias": "contact", "entity_type_id": 3},
            ],
            "relation_refs": [
                {"relation_code": relation_code, "from_alias": "deal", "to_alias": "contact"}
            ],
        }
    )


def test_catalog_descriptor_and_search(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    # denorm + multifields present
    assert catalog.get(ENTITY_DEAL, "TITLE") is not None
    assert catalog.get(ENTITY_DEAL, "PHONE") is not None
    assert catalog.get(ENTITY_DEAL, "PHONE").sensitive is True
    # data type for amount
    assert catalog.get(ENTITY_DEAL, "OPPORTUNITY").data_type == "number"
    # allowed ops differ by type: number supports gt, enumeration does not
    assert "gt" in catalog.get(ENTITY_DEAL, "OPPORTUNITY").allowed_filter_ops
    assert "gt" not in catalog.get(ENTITY_DEAL, "CATEGORY_ID").allowed_filter_ops
    # search
    hits = catalog.search("title", entity_type_id=ENTITY_DEAL)
    assert any(h.field_code == "TITLE" for h in hits)
    # snapshot hash stable across reload
    h1 = catalog.snapshot_hash()
    h2 = FieldCatalog.load(db_session, PORTAL).snapshot_hash()
    assert h1 == h2


def _rows_plan(category=1):
    return ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Deals",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {"field": {"entity_type_id": 2, "field_code": "CATEGORY_ID", "source_alias": "deal"}, "op": "eq", "value": category}
                    ],
                    "limit": 100,
                }
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
                            {"id": "idc", "header": "ID", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"}}},
                            {"id": "title", "header": "Назв", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"}}},
                        ],
                    }
                ],
            },
        }
    )


def test_compiler_counts_and_filters(db_session):
    _seed_deal(db_session, 1, category_id=1)
    _seed_deal(db_session, 2, category_id=1)
    _seed_deal(db_session, 3, category_id=9)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = _rows_plan(category=1)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 2
    rows = compiler.fetch_page(compiled, limit=10)
    assert len(rows) == 2
    assert all(r["deal"].category_id == 1 for r in rows)


def test_compiler_stage_id_integer_filter_coerced(db_session):
    _seed_deal(db_session, 1, category_id=15, stage_id="7")
    _seed_deal(db_session, 2, category_id=15, stage_id="C15:NEW")
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Deals by stage",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": 7,
                        }
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "stage",
                                "header": "Стадия",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 1

    plan_bitrix = ExportPlan2.model_validate(
        {
            **plan.model_dump(mode="json"),
            "datasets": [
                {
                    **plan.datasets[0].model_dump(mode="json"),
                    "filters": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": "C15:NEW",
                        }
                    ],
                }
            ],
        }
    )
    compiled_bitrix = compiler.compile_dataset(plan_bitrix.datasets[0])
    assert compiler.count(compiled_bitrix) == 1


def test_compiler_date_create_string_filter_coerced(db_session):
    _seed_deal(
        db_session,
        1,
        category_id=15,
        created_time=datetime(2021, 6, 15, 12, 0, tzinfo=timezone.utc),
    )
    _seed_deal(
        db_session,
        2,
        category_id=15,
        created_time=datetime(2020, 6, 15, 12, 0, tzinfo=timezone.utc),
    )
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Deals 2021",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
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
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "title",
                                "header": "Название",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 1


def test_compiler_legacy_null_category_matches_kp_filter(db_session):
    _seed_deal(db_session, 1, category_id=15, stage_id="7")
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
    other = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=1999,
        title="Other funnel",
        category_id=11,
        stage_id="7",
        payload_hash="other7",
        raw_payload={"id": 1999, "stageId": "7", "categoryId": 11},
    )
    db_session.add(other)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "KP legacy",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "CATEGORY_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": 15,
                        },
                        {
                            "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": 7,
                        },
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "id",
                                "header": "ID",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 2
    rows = compiler.fetch_page(compiled, limit=10)
    entity_ids = {r["deal"].entity_id for r in rows}
    assert 1955 in entity_ids
    assert 1999 not in entity_ids


def test_compiler_region_spb_matches_title_synonym(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    catalog.fields[(ENTITY_DEAL, "UF_CRM_5ECE25C5D78E0")] = FieldCatalogEntry(
        entity_type_id=ENTITY_DEAL,
        field_code="UF_CRM_5ECE25C5D78E0",
        display_name="Регион",
        field_type="iblock_element",
        is_custom=True,
        is_multiple=False,
        storage="jsonb",
        sensitive=False,
    )
    legacy = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=1955,
        title="КП для Метрополитена СПб",
        category_id=None,
        stage_id="7",
        payload_hash="spb1955",
        raw_payload={"id": 1955, "stageId": "7", "closed": "N"},
    )
    db_session.add(legacy)
    db_session.commit()

    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "SPb region",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {
                                "entity_type_id": 2,
                                "field_code": "UF_CRM_5ECE25C5D78E0",
                                "source_alias": "deal",
                            },
                            "op": "eq",
                            "value": 1107,
                        },
                        {
                            "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": 7,
                        },
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "id",
                                "header": "ID",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 1


def test_compiler_region_placeholder_resolves(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    catalog.fields[(ENTITY_DEAL, "UF_CRM_5ECE25C5D78E0")] = FieldCatalogEntry(
        entity_type_id=ENTITY_DEAL,
        field_code="UF_CRM_5ECE25C5D78E0",
        display_name="Регион",
        field_type="iblock_element",
        is_custom=True,
        is_multiple=False,
        storage="jsonb",
        sensitive=False,
    )
    legacy = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=1955,
        title="КП для Метрополитена СПб",
        category_id=None,
        stage_id="7",
        payload_hash="spb1955ph",
        raw_payload={"id": 1955, "stageId": "7", "closed": "N"},
    )
    db_session.add(legacy)
    db_session.commit()

    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "SPb placeholder",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {
                                "entity_type_id": 2,
                                "field_code": "UF_CRM_5ECE25C5D78E0",
                                "source_alias": "deal",
                            },
                            "op": "eq",
                            "value": "<ID региона Санкт-Петербурга>",
                        }
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "id",
                                "header": "ID",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 1


def test_compiler_unresolved_region_raises_compile_error(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    catalog.fields[(ENTITY_DEAL, "UF_CRM_5ECE25C5D78E0")] = FieldCatalogEntry(
        entity_type_id=ENTITY_DEAL,
        field_code="UF_CRM_5ECE25C5D78E0",
        display_name="Регион",
        field_type="iblock_element",
        is_custom=True,
        is_multiple=False,
        storage="jsonb",
        sensitive=False,
    )
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Bad region",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {
                                "entity_type_id": 2,
                                "field_code": "UF_CRM_5ECE25C5D78E0",
                                "source_alias": "deal",
                            },
                            "op": "eq",
                            "value": "<ID региона Неизвестный Город>",
                        }
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "id",
                                "header": "ID",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    with pytest.raises(CompileError, match="ID региона"):
        compiler.compile_dataset(plan.datasets[0])


def test_compiler_relation_join(db_session):
    _seed_contact(db_session, 50, title="Acme", phone="+7 916 123-45-67")
    _seed_deal(db_session, 1, category_id=1, contact_id=50)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    dataset = Dataset.model_validate(
        {
            "id": "d",
            "primary_entity_type_id": 2,
            "sources": [{"alias": "deal", "entity_type_id": 2}, {"alias": "contact", "entity_type_id": 3}],
            "relation_refs": [{"relation_code": "deal_contact", "from_alias": "deal", "to_alias": "contact"}],
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(dataset)
    rows = compiler.fetch_page(compiled, limit=10)
    assert len(rows) == 1
    # camelCase contactId from crm.item.list must still join (the bug being fixed)
    assert rows[0]["contact"] is not None
    assert rows[0]["contact"].title == "Acme"
    # joined contact PHONE must resolve from camelCase "phone" multifield
    from app.services.export_plan.models_v2 import FieldRef
    from app.services.intelligent_export.row_builder import get_field_raw

    phone = get_field_raw(
        rows[0], FieldRef(entity_type_id=3, field_code="PHONE", source_alias="contact"), catalog
    )
    assert phone == "+7 916 123-45-67"


def test_compiler_excludes_deleted_and_other_portal(db_session):
    d = _seed_deal(db_session, 1, category_id=1)
    d2 = _seed_deal(db_session, 2, category_id=1)
    d2.is_deleted = True
    other = _seed_deal(db_session, 3, category_id=1)
    other.portal_id = "other.portal"
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = _rows_plan(category=1)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 1  # only the non-deleted, same-portal deal


def test_compiler_viewer_scope_applied(db_session):
    _seed_deal(db_session, 1, category_id=1, assigned=439)
    _seed_deal(db_session, 2, category_id=1, assigned=999)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = _rows_plan(category=1)
    scope = ExportScope(role="viewer", assigned_by_id=439, allowed_entity_type_ids=frozenset({2, 3}), allow_sensitive_fields=False)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, scope)
    compiled = compiler.compile_dataset(plan.datasets[0])
    # viewer scope is applied in SQL regardless of plan filters
    assert compiler.count(compiled) == 1


def test_validator_rejects_unknown_field_and_bad_op(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = _rows_plan()
    # add a column with a non-existent field
    data = plan.model_dump(mode="json")
    data["workbook"]["sheets"][0]["columns"].append(
        {"id": "ghost", "header": "Ghost", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "NO_SUCH", "source_alias": "deal"}}}
    )
    # use a gt op on an enumeration field (CATEGORY_ID) -> not allowed
    data["datasets"][0]["filters"][0]["op"] = "gt"
    plan2 = ExportPlan2.model_validate(data)
    result = CatalogScopeValidator(catalog, ExportScope(role="admin")).validate(plan2)
    assert not result.valid
    codes = {i.code for i in result.issues}
    assert "FIELD_NOT_ALLOWED" in codes
    assert "FILTER_OP_NOT_ALLOWED" in codes


def test_validator_viewer_sensitive_and_scope(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = _rows_plan()
    data = plan.model_dump(mode="json")
    # viewer selecting PHONE (sensitive) without assigned filter
    data["workbook"]["sheets"][0]["columns"].append(
        {"id": "ph", "header": "Phone", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "PHONE", "source_alias": "deal"}}}
    )
    plan2 = ExportPlan2.model_validate(data)
    scope = ExportScope(role="viewer", assigned_by_id=439, allowed_entity_type_ids=frozenset({2, 3}), allow_sensitive_fields=False)
    result = CatalogScopeValidator(catalog, scope).validate(plan2)
    codes = {i.code for i in result.issues}
    assert "FIELD_NOT_ALLOWED" in codes  # PHONE sensitive denied
    assert "SCOPE_ASSIGNED_REQUIRED" in codes  # missing assigned filter


def test_apply_statement_timeout_uses_literal_ms_on_postgresql():
    from unittest.mock import MagicMock

    from app.services.export_plan.compiler_v2 import apply_statement_timeout

    db = MagicMock()
    db.bind.dialect.name = "postgresql"
    apply_statement_timeout(db, 30000)
    sql = str(db.execute.call_args[0][0])
    assert "30000ms" in sql
    assert ":ms" not in sql


def test_junction_pulls_contact_when_payload_contactid_empty(db_session):
    # Deal has contactId=0 (legacy join finds nothing) but a real CRM link exists.
    _seed_deal(db_session, 1, category_id=1, contact_id=0)
    _seed_contact(db_session, 50, title="Acme")
    _seed_link(db_session, contact_id=50, parent_id=1)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))

    # Junction relation pulls the contact.
    rows = compiler.fetch_page(compiler.compile_dataset(_link_dataset("deal_contact_link")), limit=10)
    assert len(rows) == 1
    assert rows[0]["contact"] is not None
    assert rows[0]["contact"].title == "Acme"

    # Legacy contactId join does NOT (contactId is 0).
    legacy = compiler.fetch_page(compiler.compile_dataset(_link_dataset("deal_contact")), limit=10)
    assert legacy[0]["contact"] is None


def test_junction_primary_only(db_session):
    _seed_deal(db_session, 1, category_id=1, contact_id=0)
    _seed_contact(db_session, 50, title="Primary")
    _seed_contact(db_session, 51, title="Secondary")
    _seed_link(db_session, contact_id=50, parent_id=1, is_primary=True)
    _seed_link(db_session, contact_id=51, parent_id=1, is_primary=False)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    rows = compiler.fetch_page(compiler.compile_dataset(_link_dataset("deal_primary_contact_link")), limit=10)
    assert len(rows) == 1
    assert rows[0]["contact"].title == "Primary"


def test_junction_row_explosion(db_session):
    _seed_deal(db_session, 1, category_id=1, contact_id=0)
    _seed_contact(db_session, 50, title="C1")
    _seed_contact(db_session, 51, title="C2")
    _seed_link(db_session, contact_id=50, parent_id=1)
    _seed_link(db_session, contact_id=51, parent_id=1)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    rows = compiler.fetch_page(compiler.compile_dataset(_link_dataset("deal_contact_link")), limit=10)
    assert len(rows) == 2
    assert {r["contact"].title for r in rows} == {"C1", "C2"}


def test_junction_negative_contact_id_left_join(db_session):
    # Lead-backfill style synthetic negative contact_id with no crm_entities row.
    _seed_deal(db_session, 1, category_id=1, contact_id=0)
    _seed_link(db_session, contact_id=-999, parent_id=1)
    db_session.commit()

    catalog = FieldCatalog.load(db_session, PORTAL)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    rows = compiler.fetch_page(compiler.compile_dataset(_link_dataset("deal_contact_link")), limit=10)
    assert len(rows) == 1
    assert rows[0]["contact"] is None  # left join, no matching contact entity


def test_deals_with_contacts_template_compiles(db_session):
    from app.models import CrmFieldDefinition
    from app.services.intelligent_export.templates import build_template_plan

    for code in ("NAME", "LAST_NAME", "SECOND_NAME", "POST", "COMMENTS"):
        db_session.add(
            CrmFieldDefinition(
                portal_id=PORTAL,
                entity_type_id=ENTITY_CONTACT,
                original_field_name=code,
                upper_name=code,
                field_type="string",
                is_active=True,
            )
        )
    db_session.commit()

    plan_dict = build_template_plan("deals_with_contacts", max_rows=100)
    assert plan_dict is not None
    plan = ExportPlan2.model_validate(plan_dict)
    catalog = FieldCatalog.load(db_session, PORTAL)
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiler.compile_dataset(plan.datasets[0])  # must not raise


def test_compiler_legacy_category_15_includes_stage_9_deal(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    legacy = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=2459,
        title="ЗЦ ППиМТ ... Москва-Владивосток",
        category_id=None,
        stage_id="9",
        payload_hash="deal2459",
        raw_payload={"id": 2459, "stageId": "9", "closed": "N"},
    )
    other = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=9999,
        title="Other deal",
        category_id=None,
        stage_id="99",
        payload_hash="deal9999",
        raw_payload={"id": 9999, "stageId": "99", "closed": "N"},
    )
    db_session.add_all([legacy, other])
    db_session.commit()

    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "KP stage 9",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "CATEGORY_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": 15,
                        },
                        {
                            "field": {"entity_type_id": 2, "field_code": "STAGE_ID", "source_alias": "deal"},
                            "op": "eq",
                            "value": "9",
                        },
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "id",
                                "header": "ID",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    compiled = compiler.compile_dataset(plan.datasets[0])
    assert compiler.count(compiled) == 1
    rows = compiler.fetch_page(compiled, limit=10)
    assert rows[0]["deal"].entity_id == 2459


def test_compiler_invalid_date_raises_compile_error(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Bad date",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": 2,
                    "sources": [{"alias": "deal", "entity_type_id": 2}],
                    "filters": [
                        {
                            "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
                            "op": "gte",
                            "value": "not-a-date",
                        }
                    ],
                    "limit": 100,
                }
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
                            {
                                "id": "id",
                                "header": "ID",
                                "value": {
                                    "kind": "field",
                                    "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"},
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )
    compiler = ExportPlanCompilerV2(db_session, PORTAL, catalog, ExportScope(role="admin"))
    with pytest.raises(CompileError, match="Некорректная дата"):
        compiler.compile_dataset(plan.datasets[0])
