from __future__ import annotations

from app.config import get_settings
from app.models import ENTITY_COMPANY, ENTITY_DEAL, CrmContactPhone, CrmEntity
from app.repositories.contact_repository import ContactRepository
from app.services.auth_service import resolve_portal_id
from app.services.company_tomoru_export import build_companies_without_deals_export


def _entity(db, portal_id: str, entity_type: int, entity_id: int, raw_payload: dict):
    row = CrmEntity(
        portal_id=portal_id,
        entity_type_id=entity_type,
        entity_id=entity_id,
        entity_kind="company" if entity_type == ENTITY_COMPANY else "deal",
        title=f"Entity {entity_id}",
        category_id=15 if entity_type == ENTITY_DEAL else None,
        stage_id="C15:NEW" if entity_type == ENTITY_DEAL else None,
        raw_payload=raw_payload,
        payload_hash=f"hash-{entity_type}-{entity_id}",
    )
    db.add(row)
    db.flush()
    return row


def test_company_export_excludes_companies_linked_to_deals(db_session):
    portal_id = resolve_portal_id(get_settings())
    _entity(db_session, portal_id, ENTITY_COMPANY, 10, {"PHONE": [{"VALUE": "8 916 111-22-33"}]})
    _entity(db_session, portal_id, ENTITY_COMPANY, 20, {"PHONE": [{"VALUE": "8 916 999-88-77"}]})
    _entity(db_session, portal_id, ENTITY_DEAL, 100, {"COMPANY_ID": 20})
    contact = ContactRepository(db_session, portal_id).upsert_contact(
        501,
        {"full_name": "Контакт", "company_id": 10},
        raw_payload={"ID": 501},
    )
    db_session.add(
        CrmContactPhone(
            portal_id=portal_id,
            contact_id=contact.contact_id,
            value="+7 921 555-44-33",
            value_type="MOBILE",
            is_primary=True,
        )
    )
    db_session.commit()

    result = build_companies_without_deals_export(db_session, portal_id)

    assert result.companies_total == 2
    assert result.companies_without_deals == 1
    assert result.companies_with_phones == 1
    assert result.phones == ["79161112233", "79215554433"]


def test_company_export_download_endpoint(client, db_session):
    portal_id = resolve_portal_id(get_settings())
    _entity(db_session, portal_id, ENTITY_COMPANY, 30, {"PHONE": [{"VALUE": "+7 999 123-45-67"}]})
    db_session.commit()

    response = client.get("/exports/tomoru/companies-without-deals/download")

    assert response.status_code == 200
    assert response.headers["x-companies-without-deals"] == "1"
    assert response.headers["x-export-phones"] == "1"
    assert response.content.decode("utf-8-sig").splitlines() == ["phone_number", "79991234567"]
