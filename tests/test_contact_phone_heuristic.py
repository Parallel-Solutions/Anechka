"""Tests for Tomoru contact/phone heuristic in intelligent export."""

from __future__ import annotations

from app.config import get_settings
from app.models import (
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    ENTITY_COMPANY,
    ENTITY_DEAL,
)
from app.repositories.contact_repository import ContactRepository
from app.services.export_plan.catalog import FieldCatalog, FieldCatalogEntry
from app.services.export_plan.catalog_validator import CatalogScopeValidator
from app.services.export_plan.models_v2 import ExportPlan2, SheetPostProcess
from app.services.export_plan.validator import ExportScope
from app.services.intelligent_export.company_contact_enricher import enrich_company_contacts_for_deals
from app.services.intelligent_export.contact_lpr_classifier import KeywordLprClassifier, LprPickResult
from app.services.intelligent_export.contact_phone_heuristic import (
    ContactCandidate,
    build_tomoru_phone_rows,
    detect_architect,
    is_deal_archived,
    is_deal_in_category,
    pick_company_phone,
    pick_contact_for_deal,
    pick_phone_for_contact,
)
from app.services.intelligent_export.plan_enricher import enrich_plan
from app.services.lpr_service import LprConfig

PORTAL = "tomoru.test.bitrix24.ru"


def _tomoru_deal_catalog() -> FieldCatalog:
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
            column_name={"CATEGORY_ID": "category_id", "STAGE_ID": "stage_id"}.get(code),
            sensitive=False,
        )
    return catalog


def _deal_entity(
    db,
    deal_id: int,
    *,
    closed: str = "N",
    company_id: int | None = None,
    category_id: int | None = 15,
    stage_id: str | None = None,
    title: str | None = None,
) -> CrmEntity:
    payload = {"id": deal_id, "closed": closed, "CLOSED": closed}
    if category_id is not None:
        payload["categoryId"] = category_id
        payload["CATEGORY_ID"] = category_id
    if company_id:
        payload["companyId"] = company_id
        payload["COMPANY_ID"] = company_id
    if stage_id is not None:
        payload["stageId"] = stage_id
        payload["STAGE_ID"] = stage_id
    e = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        entity_id=deal_id,
        entity_kind="deal",
        title=title or f"Сделка {deal_id}",
        category_id=category_id,
        stage_id=stage_id,
        raw_payload=payload,
        payload_hash=f"h{deal_id}",
    )
    db.add(e)
    db.flush()
    return e


def _contact(
    db,
    contact_id: int,
    *,
    post: str = "",
    full_name: str = "",
    company_id: int | None = None,
    date_create: str = "2024-01-01T10:00:00+03:00",
) -> CrmContact:
    repo = ContactRepository(db, PORTAL)
    c = repo.upsert_contact(
        contact_id,
        {
            "full_name": full_name,
            "post": post,
            "company_id": company_id,
        },
        raw_payload={
            "id": contact_id,
            "DATE_CREATE": date_create,
            "POST": post,
        },
    )
    return c


def _link(db, contact_id: int, deal_id: int) -> None:
    ContactRepository(db, PORTAL).upsert_link(contact_id, ENTITY_DEAL, deal_id, is_primary=False)


def _phone(db, contact_id: int, value: str, value_type: str) -> None:
    db.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=contact_id,
            value=value,
            value_type=value_type,
            is_primary=value_type == "MOBILE",
        )
    )
    db.flush()


def _company_entity(db, company_id: int, *, phone: str | None = None) -> CrmEntity:
    payload: dict = {"id": company_id, "TITLE": f"Компания {company_id}"}
    if phone:
        payload["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    e = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_COMPANY,
        entity_id=company_id,
        entity_kind="company",
        title=f"Компания {company_id}",
        raw_payload=payload,
        payload_hash=f"hc{company_id}",
    )
    db.add(e)
    db.flush()
    return e


def _post_process(**kwargs) -> SheetPostProcess:
    return SheetPostProcess(op="tomoru_phones", **kwargs)


def _lpr_config() -> LprConfig:
    return LprConfig(keywords=["директор"], fields=["POST"], stopwords=["уволен"])


class FixedLprClassifier:
    def __init__(self, contact_id: int | None):
        self.contact_id = contact_id

    def pick_lpr(self, candidates, *, deal_title: str = "") -> LprPickResult:
        return LprPickResult(contact_id=self.contact_id, reason="mock LPR")


def test_is_deal_archived():
    deal_open = CrmEntity(raw_payload={"closed": "N"})
    deal_closed = CrmEntity(raw_payload={"CLOSED": "Y"})
    assert is_deal_archived(deal_open) is False
    assert is_deal_archived(deal_closed) is True


def test_detect_architect():
    c = CrmContact(post="Главный архитектор", full_name="Иван")
    assert detect_architect(ContactCandidate(contact=c)) is True
    c2 = CrmContact(post="Менеджер", full_name="Пётр")
    assert detect_architect(ContactCandidate(contact=c2)) is False


def test_pick_architect_over_lpr(db_session):
    deal_id = 100
    _contact(db_session, 1, post="Менеджер", full_name="Менеджер", date_create="2024-06-01T10:00:00+03:00")
    _contact(db_session, 2, post="Архитектор проекта", full_name="Арх", date_create="2024-01-01T10:00:00+03:00")
    _link(db_session, 1, deal_id)
    _link(db_session, 2, deal_id)
    deal = _deal_entity(db_session, deal_id)
    db_session.commit()

    repo = ContactRepository(db_session, PORTAL)
    candidates = [
        ContactCandidate(contact=row["contact"], link=row["link"])
        for row in repo.get_contacts_for_parent(ENTITY_DEAL, deal_id)
    ]
    chosen, reason = pick_contact_for_deal(
        candidates,
        lpr_config=_lpr_config(),
        classifier=FixedLprClassifier(contact_id=1),
        deal_title=deal.title or "",
    )
    assert chosen is not None
    assert chosen.contact_id == 2
    assert "архитектор" in reason.lower()


def test_pick_lpr_when_no_architect(db_session):
    deal_id = 101
    _contact(db_session, 10, post="Менеджер", date_create="2024-01-01T10:00:00+03:00")
    _contact(db_session, 11, post="Генеральный директор", date_create="2024-06-01T10:00:00+03:00")
    _link(db_session, 10, deal_id)
    _link(db_session, 11, deal_id)
    db_session.commit()

    repo = ContactRepository(db_session, PORTAL)
    candidates = [
        ContactCandidate(contact=row["contact"], link=row["link"])
        for row in repo.get_contacts_for_parent(ENTITY_DEAL, deal_id)
    ]
    chosen, _reason = pick_contact_for_deal(
        candidates,
        lpr_config=_lpr_config(),
        classifier=FixedLprClassifier(contact_id=11),
    )
    assert chosen is not None
    assert chosen.contact_id == 11


def test_pick_last_contact_fallback(db_session):
    deal_id = 102
    _contact(db_session, 20, post="Менеджер", date_create="2024-01-01T10:00:00+03:00")
    _contact(db_session, 21, post="Специалист", date_create="2024-09-01T10:00:00+03:00")
    _link(db_session, 20, deal_id)
    _link(db_session, 21, deal_id)
    db_session.commit()

    repo = ContactRepository(db_session, PORTAL)
    candidates = [
        ContactCandidate(contact=row["contact"], link=row["link"])
        for row in repo.get_contacts_for_parent(ENTITY_DEAL, deal_id)
    ]
    chosen, reason = pick_contact_for_deal(
        candidates,
        lpr_config=_lpr_config(),
        classifier=FixedLprClassifier(contact_id=None),
    )
    assert chosen is not None
    assert chosen.contact_id == 21
    assert "последний" in reason


def test_mobile_before_work_phone(db_session):
    _contact(db_session, 30, full_name="Тест")
    _phone(db_session, 30, "84951234567", "WORK")
    _phone(db_session, 30, "89161234567", "MOBILE")
    db_session.commit()
    phone = pick_phone_for_contact(db_session, PORTAL, 30)
    assert phone == "79161234567"


def test_build_tomoru_skips_archived_and_dedups(db_session):
    deal1 = _deal_entity(db_session, 201, closed="N")
    deal2 = _deal_entity(db_session, 202, closed="N")
    deal3 = _deal_entity(db_session, 203, closed="Y")
    _contact(db_session, 40, post="Архитектор", date_create="2024-01-01T10:00:00+03:00")
    _contact(db_session, 41, post="Архитектор", date_create="2024-01-01T10:00:00+03:00")
    _link(db_session, 40, 201)
    _link(db_session, 41, 202)
    _phone(db_session, 40, "89161111111", "MOBILE")
    _phone(db_session, 41, "89161111111", "MOBILE")
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal1}, {"deal": deal2}, {"deal": deal3}],
        post_process=_post_process(),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "79161111111"
    assert stats.deals_skipped_archived == 1
    assert stats.phones_deduped == 1


def test_build_tomoru_skips_wrong_category(db_session):
    deal_kp = _deal_entity(db_session, 301, category_id=15)
    deal_other = _deal_entity(db_session, 302, category_id=11)
    _contact(db_session, 50, post="Архитектор")
    _contact(db_session, 51, post="Архитектор")
    _link(db_session, 50, 301)
    _link(db_session, 51, 302)
    _phone(db_session, 50, "89162222222", "MOBILE")
    _phone(db_session, 51, "89163333333", "MOBILE")
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal_kp}, {"deal": deal_other}],
        post_process=_post_process(),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "79162222222"
    assert stats.deals_skipped_wrong_category == 1


def test_enricher_tomoru_mode_adds_closed_filter():
    catalog = FieldCatalog(portal_id=PORTAL)
    catalog.fields[(ENTITY_DEAL, "CLOSED")] = FieldCatalogEntry(
        entity_type_id=ENTITY_DEAL,
        field_code="CLOSED",
        display_name="CLOSED",
        field_type="boolean",
        is_custom=False,
        is_multiple=False,
        storage="jsonb",
        sensitive=False,
    )
    catalog.fields[(ENTITY_DEAL, "CATEGORY_ID")] = FieldCatalogEntry(
        entity_type_id=ENTITY_DEAL,
        field_code="CATEGORY_ID",
        display_name="CATEGORY_ID",
        field_type="enumeration",
        is_custom=False,
        is_multiple=False,
        storage="column",
        column_name="category_id",
        sensitive=False,
    )
    plan = {
        "schema_version": "2.0",
        "title": "Tomoru",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [
                    {"alias": "deal", "entity_type_id": ENTITY_DEAL},
                    {"alias": "contact", "entity_type_id": 3},
                ],
                "relation_refs": [
                    {"relation_code": "deal_contact_link", "from_alias": "deal", "to_alias": "contact"}
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
                }
            ],
        },
    }
    enriched = enrich_plan(plan, user_message="выгрузка для тумороу", catalog=catalog)
    sheet = enriched["workbook"]["sheets"][0]
    assert sheet["post_process"]["op"] == "tomoru_phones"
    assert sheet["post_process"]["category_id"] == 15
    assert enriched["datasets"][0]["relation_refs"] == []
    assert enriched["workbook"]["include_errors_sheet"] is False
    filters = enriched["datasets"][0]["filters"]
    assert any(
        f.get("field", {}).get("field_code") == "CLOSED"
        and f.get("value") == "N"
        for f in filters
    )
    assert any(
        f.get("field", {}).get("field_code") == "CATEGORY_ID"
        and f.get("value") == 15
        for f in filters
    )


def test_is_deal_in_category_legacy_null_category_stage_3(db_session):
    deal = _deal_entity(db_session, 2566, category_id=None, stage_id="3")
    assert is_deal_in_category(deal, 15) is True


def test_is_deal_in_category_legacy_null_category_stage_7(db_session):
    deal = _deal_entity(db_session, 1955, category_id=None, stage_id="7")
    assert is_deal_in_category(deal, 15) is True


def test_is_deal_in_category_treats_zero_category_as_unset(db_session):
    deal = _deal_entity(db_session, 1955, category_id=0, stage_id="7")
    deal.raw_payload["categoryId"] = 0
    assert is_deal_in_category(deal, 15) is True


def test_build_tomoru_accepts_legacy_null_category_stage_7(db_session):
    deal = _deal_entity(
        db_session,
        1955,
        category_id=None,
        stage_id="7",
        title="КП для Метрополитена СПб",
    )
    _contact(db_session, 380, post="Архитектор")
    _link(db_session, 380, 1955)
    _phone(db_session, 380, "88123019899", "WORK")
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal}],
        post_process=_post_process(),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "78123019899"
    assert stats.deals_skipped_wrong_category == 0


def test_enricher_tomoru_orenburg_strips_wrong_llm_region():
    catalog = _tomoru_deal_catalog()
    plan = {
        "schema_version": "2.0",
        "title": "Tomoru Orenburg",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                "filters": [
                    {
                        "field": {
                            "entity_type_id": ENTITY_DEAL,
                            "field_code": "UF_CRM_5ECE25C5D78E0",
                            "source_alias": "deal",
                        },
                        "op": "eq",
                        "value": 1105,
                    }
                ],
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [{"id": "main", "name": "Номера", "mode": "rows", "dataset_id": "deals", "columns": []}],
        },
    }
    enriched = enrich_plan(
        plan,
        user_message="туморoу, Оренбург, стадия КП ушло - дошло ли КП?",
        catalog=catalog,
    )
    filters = enriched["datasets"][0]["filters"]
    assert not any(
        f.get("field", {}).get("field_code") == "UF_CRM_5ECE25C5D78E0" and f.get("value") == 1105
        for f in filters
    )
    assert any(
        f.get("field", {}).get("field_code") == "UF_CRM_5ECE25C5D78E0"
        and f.get("value") == "__tomoru_region:orenburg__"
        for f in filters
    )


def test_validator_accepts_orenburg_region_sentinel():
    catalog = _tomoru_deal_catalog()
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Tomoru Orenburg",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": ENTITY_DEAL,
                    "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                    "filters": [
                        {
                            "field": {
                                "entity_type_id": ENTITY_DEAL,
                                "field_code": "UF_CRM_5ECE25C5D78E0",
                                "source_alias": "deal",
                            },
                            "op": "eq",
                            "value": "__tomoru_region:orenburg__",
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
                        "name": "Номера",
                        "mode": "rows",
                        "dataset_id": "deals",
                        "columns": [
                            {
                                "id": "phone",
                                "header": "Телефон",
                                "value": {"kind": "constant", "value": ""},
                            }
                        ],
                    }
                ],
            },
        }
    )
    result = CatalogScopeValidator(catalog, ExportScope(role="admin")).validate(plan)
    assert result.valid
    assert not any(i.code == "FILTER_VALUE_INVALID" for i in result.issues)


def test_validator_rejects_invalid_region_value():
    catalog = _tomoru_deal_catalog()
    plan = ExportPlan2.model_validate(
        {
            "schema_version": "2.0",
            "title": "Bad region",
            "datasets": [
                {
                    "id": "deals",
                    "primary_entity_type_id": ENTITY_DEAL,
                    "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                    "filters": [
                        {
                            "field": {
                                "entity_type_id": ENTITY_DEAL,
                                "field_code": "UF_CRM_5ECE25C5D78E0",
                                "source_alias": "deal",
                            },
                            "op": "eq",
                            "value": "not-a-region",
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
                        "name": "Номера",
                        "mode": "rows",
                        "dataset_id": "deals",
                        "columns": [],
                    }
                ],
            },
        }
    )
    result = CatalogScopeValidator(catalog, ExportScope(role="admin")).validate(plan)
    assert not result.valid
    assert any(i.code == "FILTER_VALUE_INVALID" for i in result.issues)


def test_enriched_orenburg_plan_passes_validation():
    catalog = _tomoru_deal_catalog()
    plan = {
        "schema_version": "2.0",
        "title": "Tomoru Orenburg",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                "filters": [
                    {
                        "field": {
                            "entity_type_id": ENTITY_DEAL,
                            "field_code": "UF_CRM_5ECE25C5D78E0",
                            "source_alias": "deal",
                        },
                        "op": "eq",
                        "value": 1105,
                    }
                ],
                "limit": 100,
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [{"id": "main", "name": "Номера", "mode": "rows", "dataset_id": "deals", "columns": []}],
        },
    }
    enriched = enrich_plan(
        plan,
        user_message="туморoу, Оренбург, стадия КП ушло - дошло ли КП?",
        catalog=catalog,
    )
    validated = ExportPlan2.model_validate(enriched)
    result = CatalogScopeValidator(catalog, ExportScope(role="admin")).validate(validated)
    assert result.valid, result.issues
    assert not any(i.code == "FILTER_VALUE_INVALID" for i in result.issues)


def test_enricher_tomoru_spb_region_uses_uf_not_title():
    catalog = FieldCatalog(portal_id=PORTAL)
    for code, storage in (
        ("CLOSED", "jsonb"),
        ("CATEGORY_ID", "column"),
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
            column_name="category_id" if code == "CATEGORY_ID" else None,
            sensitive=False,
        )
    plan = {
        "schema_version": "2.0",
        "title": "Tomoru SPb",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": ENTITY_DEAL,
                "sources": [{"alias": "deal", "entity_type_id": ENTITY_DEAL}],
                "filters": [
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
                ],
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [{"id": "main", "name": "Номера", "mode": "rows", "dataset_id": "deals", "columns": []}],
        },
    }
    enriched = enrich_plan(
        plan,
        user_message="тумороу, санкт-петербург, стадия сделки 7",
        catalog=catalog,
    )
    filters = enriched["datasets"][0]["filters"]
    assert not any(
        f.get("field", {}).get("field_code") == "TITLE"
        and "Санкт" in str(f.get("value", ""))
        for f in filters
    )
    assert any(
        f.get("field", {}).get("field_code") == "UF_CRM_5ECE25C5D78E0"
        and f.get("value") == 1107
        for f in filters
    )


def test_build_tomoru_uses_company_phone_when_no_contacts(db_session):
    company_id = 9001
    deal = _deal_entity(db_session, 153, company_id=company_id)
    _company_entity(db_session, company_id, phone="88121234567")
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal}],
        post_process=_post_process(fetch_company_contacts_live=False),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "78121234567"
    assert stats.deals_used_company_phone == 1


def test_build_tomoru_company_phone_when_contact_has_no_phone(db_session):
    company_id = 9002
    deal = _deal_entity(db_session, 154, company_id=company_id)
    _company_entity(db_session, company_id, phone="88129876543")
    _contact(db_session, 60, post="Менеджер", company_id=company_id)
    _link(db_session, 60, 154)
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal}],
        post_process=_post_process(fetch_company_contacts_live=False),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "78129876543"
    assert stats.deals_used_company_phone == 1


def test_build_tomoru_prefers_contact_phone_over_company_phone(db_session):
    company_id = 9003
    deal = _deal_entity(db_session, 155, company_id=company_id)
    _company_entity(db_session, company_id, phone="88120000000")
    _contact(db_session, 61, post="Архитектор", company_id=company_id)
    _link(db_session, 61, 155)
    _phone(db_session, 61, "89161112233", "MOBILE")
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal}],
        post_process=_post_process(fetch_company_contacts_live=False),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "79161112233"
    assert stats.deals_used_company_phone == 0


def test_pick_company_phone_from_string_phone_payload(db_session):
    payload = {"id": 90407, "phone": "83532987871", "TITLE": "Департамент Оренбурга"}
    e = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=ENTITY_COMPANY,
        entity_id=90407,
        entity_kind="company",
        title="Департамент Оренбурга",
        raw_payload=payload,
        payload_hash="h90407",
    )
    db_session.add(e)
    db_session.commit()
    phone = pick_company_phone(db_session, PORTAL, 90407)
    assert phone == "73532987871"


def test_build_tomoru_deal2566_department_contact_falls_back_to_company_phone(db_session):
    """Отдел без телефона → телефон организации (сценарий сделки 2566)."""
    deal = _deal_entity(
        db_session,
        2566,
        category_id=None,
        stage_id="3",
        company_id=407,
        title="КП внесение изменений в МНГП города Оренбурга",
    )
    company_payload = {
        "id": 407,
        "phone": "83532987871",
        "TITLE": "Департамент градостроительства администрации города Оренбурга",
    }
    db_session.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_COMPANY,
            entity_id=407,
            entity_kind="company",
            title=company_payload["TITLE"],
            raw_payload=company_payload,
            payload_hash="h407",
        )
    )
    _contact(db_session, 464, full_name="Отдел Контрактной службы", company_id=407)
    _link(db_session, 464, 2566)
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal}],
        post_process=_post_process(fetch_company_contacts_live=False),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "73532987871"
    assert stats.deals_used_company_phone == 1


def test_pick_company_phone_from_entity(db_session):
    _company_entity(db_session, 9010, phone="84957777777")
    db_session.commit()
    phone = pick_company_phone(db_session, PORTAL, 9010)
    assert phone == "74957777777"


class MockBitrixClient:
    def __init__(self):
        self.saved_contact_ids: list[int] = []

    def get_company_contacts(self, company_id: int) -> list[int]:
        assert company_id == 9100
        return [701, 702]

    def get_contact(self, contact_id: int) -> dict | None:
        self.saved_contact_ids.append(contact_id)
        return {
            "ID": contact_id,
            "NAME": f"Contact {contact_id}",
            "COMPANY_ID": 9100,
            "PHONE": [{"VALUE": f"8916{contact_id:07d}", "VALUE_TYPE": "MOBILE"}],
        }

    def get_company(self, company_id: int) -> dict | None:
        return {"ID": company_id, "TITLE": "Test Co"}


def test_enrich_company_contacts_for_deals_saves_contacts(db_session):
    deal = _deal_entity(db_session, 160, company_id=9100)
    db_session.commit()
    client = MockBitrixClient()

    saved = enrich_company_contacts_for_deals(
        db_session,
        PORTAL,
        [{"deal": deal}],
        bitrix_client=client,
    )
    assert saved == 2
    assert client.saved_contact_ids == [701, 702]
    repo = ContactRepository(db_session, PORTAL)
    contacts = repo.get_contacts_by_company_id(9100)
    assert len(contacts) == 2


def test_is_deal_in_category_legacy_null_category_stage_9(db_session):
    deal = _deal_entity(db_session, 2459, category_id=None, stage_id="9")
    assert is_deal_in_category(deal, 15) is True


def test_build_tomoru_deal_2459_phone_via_payload_contact(db_session):
    deal = _deal_entity(
        db_session,
        2459,
        category_id=None,
        stage_id="9",
        company_id=399,
        title="ЗЦ ППиМТ для ЛО ТИ Строительство ... Москва-Владивосток",
    )
    deal.raw_payload["contactId"] = 451
    deal.raw_payload["CONTACT_ID"] = 451
    db_session.flush()
    _contact(db_session, 451, full_name="Старовойтова", company_id=399)
    _phone(db_session, 451, "83452490276", "WORK")
    db_session.commit()

    rows, stats = build_tomoru_phone_rows(
        db_session,
        PORTAL,
        [{"deal": deal}],
        post_process=_post_process(),
        lpr_config=_lpr_config(),
        settings=get_settings(),
        classifier=KeywordLprClassifier(_lpr_config()),
    )
    assert len(rows) == 1
    assert rows[0]["phone"] == "73452490276"
    assert stats.deals_skipped_wrong_category == 0
