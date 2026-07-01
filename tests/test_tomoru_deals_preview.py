"""Tests for Tomoru deals preview API and Bitrix URLs."""



from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.models import CrmEntity, ENTITY_COMPANY, ENTITY_DEAL
from app.models import CrmDictionary, CrmDictionaryEntry
from app.repositories.contact_repository import ContactRepository
from app.utils.portal import (
    bitrix_company_url,
    bitrix_contact_url,
    bitrix_deal_url,
    portal_id_from_webhook,
)





def _portal() -> str:

    return portal_id_from_webhook(get_settings().bitrix_webhook_url)





def test_bitrix_deal_url():

    assert bitrix_deal_url("bitrix24.parresh.ru", 12345) == (

        "https://bitrix24.parresh.ru/crm/deal/details/12345/"

    )

    assert bitrix_deal_url("default", 1) is None

    assert bitrix_deal_url("", 1) is None


def test_bitrix_contact_url():
    assert bitrix_contact_url("bitrix24.parresh.ru", 555) == (
        "https://bitrix24.parresh.ru/crm/contact/details/555/"
    )
    assert bitrix_contact_url("default", 1) is None


def test_bitrix_company_url():
    assert bitrix_company_url("bitrix24.parresh.ru", 777) == (
        "https://bitrix24.parresh.ru/crm/company/details/777/"
    )
    assert bitrix_company_url("default", 1) is None


def test_api_tomoru_deals_includes_company_phone_and_description(client, db_session):
    portal = _portal()
    deal_id = 820
    company_id = 8200
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal with company",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h820",
            raw_payload={
                "id": deal_id,
                "title": "Deal with company",
                "COMPANY_ID": company_id,
                "closed": "N",
            },
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_COMPANY,
            entity_id=company_id,
            entity_kind="company",
            title="ООО Тест",
            payload_hash="hc8200",
            raw_payload={
                "ID": company_id,
                "TITLE": "ООО Тест",
                "PHONE": [{"VALUE": "84951234567", "VALUE_TYPE": "WORK"}],
                "COMMENTS": "Описание компании для сделки",
            },
        )
    )
    db_session.commit()

    resp = client.get("/api/tomoru/deals?category_id=15")
    assert resp.status_code == 200
    data = resp.json()
    deal = next(d for d in data["deals"] if d["deal_id"] == deal_id)
    company = deal["company"]
    assert company is not None
    assert company["company_id"] == company_id
    assert company["title"] == "ООО Тест"
    assert company["description"] == "Описание компании для сделки"
    assert company["phone"] == "74951234567"
    if portal != "default":
        assert company["bitrix_url"] == bitrix_company_url(portal, company_id)


def test_api_tomoru_deals_includes_stage_name(client, db_session):
    portal = _portal()
    dictionary = CrmDictionary(
        portal_id=portal,
        entity_type_id=ENTITY_DEAL,
        dictionary_code="status_DEAL_STAGE_15",
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
    deal_id = 830
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal with stage name",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h830",
            raw_payload={"id": deal_id, "title": "Deal with stage name", "closed": "N"},
        )
    )
    db_session.commit()

    resp = client.get("/api/tomoru/deals?category_id=15&stage_id=C15:NEW")
    assert resp.status_code == 200
    deal = next(d for d in resp.json()["deals"] if d["deal_id"] == deal_id)
    assert deal["stage_id"] == "C15:NEW"
    assert deal["stage_name"] == "Новая"


def test_api_tomoru_deals_without_company_returns_null(client, db_session):
    portal = _portal()
    deal_id = 821
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal without company",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h821",
            raw_payload={"id": deal_id, "title": "Deal without company", "closed": "N"},
        )
    )
    db_session.commit()

    resp = client.get("/api/tomoru/deals?category_id=15")
    assert resp.status_code == 200
    data = resp.json()
    deal = next(d for d in data["deals"] if d["deal_id"] == deal_id)
    assert deal["company"] is None


def test_api_tomoru_deals_includes_contacts_with_description(client, db_session):
    portal = _portal()
    deal_id = 810
    contact_id = 8101
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal with contact",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h810",
            raw_payload={"id": deal_id, "title": "Deal with contact", "closed": "N"},
        )
    )
    db_session.commit()
    repo = ContactRepository(db_session, portal)
    repo.upsert_contact(
        contact_id,
        {"full_name": "Иванов Иван", "post": "Директор"},
        raw_payload={"id": contact_id, "COMMENTS": "Тестовое описание", "POST": "Директор"},
    )
    repo.upsert_link(contact_id, ENTITY_DEAL, deal_id, is_primary=True)
    db_session.commit()

    resp = client.get("/api/tomoru/deals?category_id=15")
    assert resp.status_code == 200
    data = resp.json()
    deal = next(d for d in data["deals"] if d["deal_id"] == deal_id)
    assert len(deal["contacts"]) == 1
    contact = deal["contacts"][0]
    assert contact["contact_id"] == contact_id
    assert contact["full_name"] == "Иванов Иван"
    assert contact["description"] == "Тестовое описание"
    assert contact["post"] == "Директор"
    assert contact["source"] == "deal"
    assert contact["is_primary"] is True
    if portal != "default":
        assert contact["bitrix_url"] == bitrix_contact_url(portal, contact_id)
    assert "эвристике" in (data["note"] or "")
    assert contact["selected_for_export"] is True


def test_api_tomoru_deals_selects_architect_for_export(client, db_session):
    portal = _portal()
    deal_id = 815
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal architect",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h815",
            raw_payload={"id": deal_id, "title": "Deal architect", "closed": "N"},
        )
    )
    db_session.commit()
    repo = ContactRepository(db_session, portal)
    repo.upsert_contact(
        8151,
        {"full_name": "Менеджер", "post": "Менеджер"},
        raw_payload={"id": 8151, "POST": "Менеджер", "DATE_CREATE": "2024-06-01T10:00:00+03:00"},
    )
    repo.upsert_contact(
        8152,
        {"full_name": "Архитектор", "post": "Архитектор проекта"},
        raw_payload={"id": 8152, "POST": "Архитектор проекта", "DATE_CREATE": "2024-01-01T10:00:00+03:00"},
    )
    repo.upsert_link(8151, ENTITY_DEAL, deal_id, is_primary=False)
    repo.upsert_link(8152, ENTITY_DEAL, deal_id, is_primary=False)
    db_session.commit()

    resp = client.get("/api/tomoru/deals?category_id=15")
    assert resp.status_code == 200
    deal = next(d for d in resp.json()["deals"] if d["deal_id"] == deal_id)
    by_id = {c["contact_id"]: c for c in deal["contacts"]}
    assert by_id[8152]["selected_for_export"] is True
    assert by_id[8151]["selected_for_export"] is False
    assert "архитектор" in (by_id[8152]["selection_reason"] or "").lower()


def test_api_tomoru_deals_returns_bitrix_url(client, db_session):

    portal = _portal()

    db_session.add(

        CrmEntity(

            portal_id=portal,

            entity_type_id=ENTITY_DEAL,

            entity_id=801,

            title="Tomoru preview deal",

            category_id=15,

            stage_id="C15:NEW",

            payload_hash="h801",

            raw_payload={"id": 801, "title": "Tomoru preview deal"},

        )

    )

    db_session.commit()



    resp = client.get("/api/tomoru/deals?category_id=15")

    assert resp.status_code == 200

    data = resp.json()

    assert data["available"] is True

    assert data["total"] >= 1

    deal = next(d for d in data["deals"] if d["deal_id"] == 801)

    if portal != "default":

        assert deal["bitrix_url"] == bitrix_deal_url(portal, 801)

    else:

        assert deal["bitrix_url"] is None





def test_api_tomoru_deals_ignores_unknown_limit_query_param(client):
    resp = client.get("/api/tomoru/deals?category_id=15&stage_id=C15:NEW&limit=999999")
    assert resp.status_code == 200





def test_api_tomoru_deals_filters_by_region(client, db_session):

    portal = _portal()

    db_session.add(

        CrmEntity(

            portal_id=portal,

            entity_type_id=ENTITY_DEAL,

            entity_id=901,

            title="Tomsk deal",

            category_id=15,

            stage_id="C15:NEW",

            payload_hash="h901",

            raw_payload={"id": 901, "title": "Tomsk deal", "UF_CRM_5ECE25C5D78E0": 1091},

        )

    )

    db_session.add(

        CrmEntity(

            portal_id=portal,

            entity_type_id=ENTITY_DEAL,

            entity_id=902,

            title="Moscow deal",

            category_id=15,

            stage_id="C15:NEW",

            payload_hash="h902",

            raw_payload={"id": 902, "title": "Moscow deal", "UF_CRM_5ECE25C5D78E0": 1105},

        )

    )

    db_session.commit()



    resp = client.get("/api/tomoru/deals?category_id=15&region_id=1091")

    assert resp.status_code == 200

    data = resp.json()

    assert data["available"] is True

    assert data["total"] == 1

    assert data["deals"][0]["deal_id"] == 901


def test_api_tomoru_deals_filters_by_multiple_regions(client, db_session):
    portal = _portal()
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=911,
            title="Tomsk deal",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h911",
            raw_payload={"id": 911, "title": "Tomsk deal", "UF_CRM_5ECE25C5D78E0": 1091},
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=912,
            title="Moscow deal",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h912",
            raw_payload={"id": 912, "title": "Moscow deal", "UF_CRM_5ECE25C5D78E0": 1105},
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=913,
            title="Other deal",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h913",
            raw_payload={"id": 913, "title": "Other deal", "UF_CRM_5ECE25C5D78E0": 1200},
        )
    )
    db_session.commit()

    resp = client.get(
        "/api/tomoru/deals?category_id=15&region_id=1091&region_id=1105"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["total"] == 2
    deal_ids = {d["deal_id"] for d in data["deals"]}
    assert deal_ids == {911, 912}


def test_api_tomoru_deals_filters_by_multiple_stages(client, db_session):
    portal = _portal()
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=921,
            title="New deal",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h921",
            raw_payload={"id": 921, "title": "New deal", "closed": "N"},
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=922,
            title="Warm deal",
            category_id=15,
            stage_id="C15:4",
            payload_hash="h922",
            raw_payload={"id": 922, "title": "Warm deal", "closed": "N"},
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=923,
            title="Lost deal",
            category_id=15,
            stage_id="C15:LOSE",
            payload_hash="h923",
            raw_payload={"id": 923, "title": "Lost deal", "closed": "Y"},
        )
    )
    db_session.commit()

    resp = client.get(
        "/api/tomoru/deals?category_id=15&stage_id=C15:NEW&stage_id=C15:4"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["total"] == 2
    deal_ids = {d["deal_id"] for d in data["deals"]}
    assert deal_ids == {921, 922}


def test_api_tomoru_deals_lead_entity_unavailable(client):

    resp = client.get("/api/tomoru/deals?entity_type=lead")

    assert resp.status_code == 200

    data = resp.json()

    assert data["available"] is False

    assert "лидов" in (data["note"] or "").lower()





def test_tomoru_export_page_has_filters(client):

    resp = client.get("/tomoru-export")

    assert resp.status_code == 200

    assert 'id="deals-preview-card"' in resp.text

    assert 'id="region-wrap"' in resp.text

    assert 'class="col-md-6 d-none" id="region-wrap"' not in resp.text

    assert 'id="ent-lead"' not in resp.text

    assert 'id="entity-type-wrap"' not in resp.text

    assert 'id="date_from"' in resp.text
    assert 'id="date_to"' in resp.text
    assert 'id="date_range"' not in resp.text
    assert 'flatpickr' in resp.text.lower()

    assert 'id="jobs-history"' in resp.text

    assert 'class="mt-5 d-none" id="jobs-history"' not in resp.text

    assert "tom-select" in resp.text.lower()
    assert 'id="stage-wrap"' in resp.text
    assert 'class="col-md-6 d-none" id="stage-wrap"' in resp.text
    assert 'id="stage_id"' in resp.text
    assert 'id="stage_id" multiple' in resp.text or 'name="stage_id" id="stage_id" multiple' in resp.text
    assert "— выберите воронку —" in resp.text
    assert "category_id: null" in resp.text
    assert "function inferCategoryId(" in resp.text
    assert "function updateStageVisibility(" in resp.text
    assert "function readCategoryFromForm(" in resp.text
    assert "history.replaceState" in resp.text
    assert "Выберите воронку" in resp.text
    assert "!== DEFAULT_CATEGORY" not in resp.text
    assert "Сделки по фильтрам" in resp.text or "ссылками на карточки" in resp.text
    assert "Сделки и контакты" in resp.text
    assert "Описание" in resp.text
    assert 'name="limit"' not in resp.text
    assert "deals-placeholder" in resp.text
    assert "Выберите стадию, регион или период" in resp.text
    assert "await loadDealsPreview()" not in resp.text
    assert "function parseFiltersFromUrl()" in resp.text
    assert "function navigateFilters(" in resp.text
    assert "function buildUrlFromFilters(" in resp.text
    assert "addEventListener('popstate'" in resp.text
    assert "async function initPage()" in resp.text
    assert "history.pushState" in resp.text


def _seed_deals_with_created_times(db_session, portal: str) -> None:
    deals = [
        (1001, "Early deal", datetime(2024, 1, 10, 12, 0, tzinfo=timezone.utc)),
        (1002, "Mid deal", datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)),
        (1003, "Late deal", datetime(2024, 12, 20, 8, 0, tzinfo=timezone.utc)),
    ]
    for deal_id, title, created_time in deals:
        db_session.add(
            CrmEntity(
                portal_id=portal,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                title=title,
                category_id=15,
                stage_id="C15:NEW",
                payload_hash=f"h{deal_id}",
                created_time=created_time,
                raw_payload={"id": deal_id, "title": title, "closed": "N"},
            )
        )
    db_session.commit()


def test_api_tomoru_deals_filters_by_partial_date_range(client, db_session):
    portal = _portal()
    _seed_deals_with_created_times(db_session, portal)

    resp_from = client.get(
        "/api/tomoru/deals?category_id=15&date_from=2024-06-15"
    )
    assert resp_from.status_code == 200
    ids_from = {d["deal_id"] for d in resp_from.json()["deals"]}
    assert ids_from == {1002, 1003}

    resp_to = client.get(
        "/api/tomoru/deals?category_id=15&date_to=2024-06-15"
    )
    assert resp_to.status_code == 200
    ids_to = {d["deal_id"] for d in resp_to.json()["deals"]}
    assert ids_to == {1001, 1002}

    resp_both = client.get(
        "/api/tomoru/deals?category_id=15&date_from=2024-06-01&date_to=2024-06-30"
    )
    assert resp_both.status_code == 200
    ids_both = {d["deal_id"] for d in resp_both.json()["deals"]}
    assert ids_both == {1002}


def test_save_contact_selection_persisted_in_preview(client, db_session):
    portal = _portal()
    deal_id = 830
    contact_a = 8301
    contact_b = 8302
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal saved selection",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h830",
            raw_payload={"id": deal_id, "title": "Deal saved selection", "closed": "N"},
        )
    )
    db_session.commit()
    repo = ContactRepository(db_session, portal)
    repo.upsert_contact(
        contact_a,
        {"full_name": "Менеджер", "post": "Менеджер"},
        raw_payload={"id": contact_a, "POST": "Менеджер"},
    )
    repo.upsert_contact(
        contact_b,
        {"full_name": "Директор", "post": "Генеральный директор"},
        raw_payload={"id": contact_b, "POST": "Генеральный директор"},
    )
    repo.upsert_link(contact_a, ENTITY_DEAL, deal_id, is_primary=False)
    repo.upsert_link(contact_b, ENTITY_DEAL, deal_id, is_primary=False)
    db_session.commit()

    save_resp = client.put(
        f"/api/tomoru/deals/{deal_id}/contact-selection",
        json={"contact_ids": [contact_a, contact_b]},
    )
    assert save_resp.status_code == 204

    resp = client.get("/api/tomoru/deals?category_id=15")
    assert resp.status_code == 200
    deal = next(d for d in resp.json()["deals"] if d["deal_id"] == deal_id)
    by_id = {c["contact_id"]: c for c in deal["contacts"]}
    assert by_id[contact_a]["selected_for_export"] is True
    assert by_id[contact_b]["selected_for_export"] is True
    assert by_id[contact_a]["selection_reason"] == "сохранённый выбор"


def test_save_empty_contact_selection_clears_checkboxes(client, db_session):
    portal = _portal()
    deal_id = 831
    contact_id = 8311
    db_session.add(
        CrmEntity(
            portal_id=portal,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal empty selection",
            category_id=15,
            stage_id="C15:NEW",
            payload_hash="h831",
            raw_payload={"id": deal_id, "title": "Deal empty selection", "closed": "N"},
        )
    )
    db_session.commit()
    repo = ContactRepository(db_session, portal)
    repo.upsert_contact(
        contact_id,
        {"full_name": "Директор", "post": "Генеральный директор"},
        raw_payload={"id": contact_id, "POST": "Генеральный директор"},
    )
    repo.upsert_link(contact_id, ENTITY_DEAL, deal_id, is_primary=True)
    db_session.commit()

    save_resp = client.put(
        f"/api/tomoru/deals/{deal_id}/contact-selection",
        json={"contact_ids": []},
    )
    assert save_resp.status_code == 204

    resp = client.get("/api/tomoru/deals?category_id=15")
    assert resp.status_code == 200
    deal = next(d for d in resp.json()["deals"] if d["deal_id"] == deal_id)
    assert deal["contacts"][0]["selected_for_export"] is False


def test_api_tomoru_deals_returns_all_matching_records(client, db_session):
    portal = _portal()
    for deal_id in range(940, 946):
        db_session.add(
            CrmEntity(
                portal_id=portal,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                title=f"Bulk deal {deal_id}",
                category_id=15,
                stage_id="C15:BULK",
                payload_hash=f"h{deal_id}",
                raw_payload={"id": deal_id, "title": f"Bulk deal {deal_id}", "closed": "N"},
            )
        )
    db_session.commit()

    resp = client.get(
        "/api/tomoru/deals?category_id=15&stage_id=C15:BULK&page_size=50"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["matched_total"] == 6
    assert data["truncated"] is False
    assert data["total"] == 6
    assert len(data["deals"]) == 6


def test_api_tomoru_deals_not_truncated_for_small_set(client, db_session):
    portal = _portal()
    for deal_id in range(950, 953):
        db_session.add(
            CrmEntity(
                portal_id=portal,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                title=f"Small deal {deal_id}",
                category_id=15,
                stage_id="C15:SMALL",
                payload_hash=f"hs{deal_id}",
                raw_payload={"id": deal_id, "title": f"Small deal {deal_id}", "closed": "N"},
            )
        )
    db_session.commit()

    resp = client.get(
        "/api/tomoru/deals?category_id=15&stage_id=C15:SMALL&page_size=50"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["matched_total"] == 3
    assert data["truncated"] is False
    assert data["total"] == 3


def test_count_entities_for_export_matches_list_filters(db_session):
    from app.repositories.crm_repository import CrmRepository

    portal = _portal()
    repo = CrmRepository(db_session, portal)
    for deal_id in range(960, 964):
        db_session.add(
            CrmEntity(
                portal_id=portal,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                title=f"Count deal {deal_id}",
                category_id=15,
                stage_id="C15:COUNT",
                payload_hash=f"hc{deal_id}",
                raw_payload={"id": deal_id, "title": f"Count deal {deal_id}", "closed": "N"},
            )
        )
    db_session.commit()

    count = repo.count_entities_for_export(
        ENTITY_DEAL,
        category_id=15,
        stage_ids=["C15:COUNT"],
    )
    rows = repo.list_entities_for_export(
        ENTITY_DEAL,
        category_id=15,
        stage_ids=["C15:COUNT"],
        limit=100,
    )
    assert count == 4
    assert len(rows) == 4
