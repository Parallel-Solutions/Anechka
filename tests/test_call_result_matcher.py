"""Unit tests for call result matcher."""

from app.models import (
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    CrmUser,
    ENTITY_DEAL,
)
from app.services.call_results.matcher import CallResultMatcher

PORTAL = "example.bitrix24.ru"


def _seed_deal(db, deal_id: int, assigned: int = 42):
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title=f"Deal {deal_id}",
            assigned_by_id=assigned,
            raw_payload={"closed": "N", "companyId": 0},
            payload_hash=f"hash-{deal_id}",
        )
    )


def _seed_contact_phone(db, contact_id: int, phone: str):
    db.add(CrmContact(portal_id=PORTAL, contact_id=contact_id, full_name="Test Contact"))
    db.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=contact_id,
            value=phone,
            value_type="MOBILE",
            is_primary=True,
        )
    )


def test_match_by_deal_id(db_session):
    _seed_deal(db_session, 1001)
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79123456789", file_deal_id=1001)
    assert r.match_status == "matched"
    assert r.matched_deal_id == 1001


def test_match_contact_single_deal(db_session):
    _seed_deal(db_session, 2001)
    _seed_contact_phone(db_session, 50, "89161112233")
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=50,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=2001,
            is_primary=True,
        )
    )
    db_session.add(CrmUser(portal_id=PORTAL, external_id=42, display_name="Manager"))
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79161112233")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 2001


def test_ambiguous_multiple_deals(db_session):
    _seed_deal(db_session, 3001)
    _seed_deal(db_session, 3002)
    _seed_contact_phone(db_session, 60, "89162223344")
    for did in (3001, 3002):
        db_session.add(
            CrmContactLink(
                portal_id=PORTAL,
                contact_id=60,
                parent_entity_type_id=ENTITY_DEAL,
                parent_entity_id=did,
                is_primary=False,
            )
        )
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79162223344")
    assert r.match_status == "ambiguous"
    assert len(r.candidates) == 2


def test_invalid_phone(db_session):
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row(None, is_valid_phone=False)
    assert r.match_status == "invalid"
