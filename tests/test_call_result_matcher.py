"""Unit tests for call result matcher."""

from app.models import (
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    CrmUser,
    ENTITY_COMPANY,
    ENTITY_DEAL,
    ExportJob,
)
from app.services.call_results.matcher import CallResultMatcher
from app.services.export_phone_registry import ExportPhoneRegistry, ExportPhoneRow

PORTAL = "example.bitrix24.ru"


def _seed_deal(db, deal_id: int, assigned: int = 42, *, closed: str = "N", company_id: int = 0):
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title=f"Deal {deal_id}",
            assigned_by_id=assigned,
            raw_payload={"closed": closed, "CLOSED": closed, "companyId": company_id},
            payload_hash=f"hash-{deal_id}",
        )
    )


def _seed_contact_phone(db, contact_id: int, phone: str, *, company_id: int | None = None):
    db.add(
        CrmContact(
            portal_id=PORTAL,
            contact_id=contact_id,
            full_name="Test Contact",
            company_id=company_id,
        )
    )
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
    assert r.match_status == "matched"
    assert r.matched_deal_id == 3002
    assert len(r.candidates) == 2
    assert "наибольшим номером" in r.match_reason


def test_invalid_phone(db_session):
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row(None, is_valid_phone=False)
    assert r.match_status == "invalid"


def test_match_by_company_phone_string(db_session):
    db_session.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_COMPANY,
            entity_id=6591,
            title="Company",
            raw_payload={"phone": "83533534219"},
            payload_hash="hash-co",
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=24146,
            title="Deal",
            assigned_by_id=42,
            raw_payload={"closed": "N", "companyId": 6591},
            payload_hash="hash-deal",
        )
    )
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("73533534219")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 24146
    assert r.matched_company_id == 6591
    assert "компании" in r.match_reason


def test_match_by_company_fm_payload(db_session):
    db_session.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_COMPANY,
            entity_id=7001,
            title="Company FM",
            raw_payload={
                "fm": [
                    {"id": 1, "value": "83533534219", "typeId": "PHONE", "valueType": "WORK"},
                ],
            },
            payload_hash="hash-co-fm",
        )
    )
    db_session.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=7002,
            title="Deal FM",
            assigned_by_id=42,
            raw_payload={"closed": "N", "companyId": 7001},
            payload_hash="hash-deal-fm",
        )
    )
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("73533534219")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 7002


def test_match_closed_deal_when_no_active(db_session):
    _seed_deal(db_session, 9001, closed="Y")
    _seed_contact_phone(db_session, 90, "89169001122")
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=90,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=9001,
            is_primary=True,
        )
    )
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79169001122")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 9001
    assert "закрытая сделка" in r.match_reason


def test_fallback_to_company_when_contact_has_no_deals(db_session):
    for deal_id in (9101, 9102, 9103):
        _seed_deal(db_session, deal_id, company_id=9100)
    _seed_contact_phone(db_session, 91, "89169002233", company_id=9100)
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79169002233")
    assert r.match_status == "matched"
    assert r.matched_contact_id == 91
    assert r.matched_company_id == 9100
    assert r.matched_deal_id == 9103
    assert len(r.candidates) == 3
    assert "наибольшим номером" in r.match_reason


def test_match_by_export_registry_when_crm_empty(db_session):
    _seed_deal(db_session, 5001)
    job = ExportJob(mode="region_lpr", status="completed", parameters_json="{}")
    db_session.add(job)
    db_session.commit()

    ExportPhoneRegistry(db_session).save_entries(
        PORTAL,
        job.id,
        "region_lpr",
        [ExportPhoneRow(phone="89165556677", deal_id=5001, contact_id=55)],
    )
    db_session.commit()

    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79165556677")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 5001
    assert r.matched_contact_id == 55
    assert r.match_reason == "Сопоставлено по реестру выгрузки"


def test_export_registry_ambiguous_multiple_deals(db_session):
    _seed_deal(db_session, 6001)
    _seed_deal(db_session, 6002)
    job1 = ExportJob(mode="region_lpr", status="completed", parameters_json="{}")
    job2 = ExportJob(mode="region_lpr", status="completed", parameters_json="{}")
    db_session.add_all([job1, job2])
    db_session.commit()

    registry = ExportPhoneRegistry(db_session)
    registry.save_entries(
        PORTAL,
        job1.id,
        "region_lpr",
        [ExportPhoneRow(phone="79167778899", deal_id=6001, contact_id=61)],
    )
    registry.save_entries(
        PORTAL,
        job2.id,
        "region_lpr",
        [ExportPhoneRow(phone="79167778899", deal_id=6002, contact_id=62)],
    )
    db_session.commit()

    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79167778899")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 6002
    assert len(r.candidates) == 2
    assert "реестре выгрузки" in r.match_reason
    assert "наибольшим номером" in r.match_reason


def test_crm_fallback_when_export_registry_misses(db_session):
    _seed_deal(db_session, 7001)
    _seed_contact_phone(db_session, 70, "89168889900")
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=70,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=7001,
            is_primary=True,
        )
    )
    db_session.commit()

    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79168889900")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 7001
    assert "контакта" in r.match_reason


def test_multiple_contacts_same_phone_different_deals_picks_max(db_session):
    _seed_deal(db_session, 2488)
    _seed_deal(db_session, 11945)
    _seed_contact_phone(db_session, 101, "89162223355")
    _seed_contact_phone(db_session, 102, "89162223355")
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=101,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=2488,
            is_primary=True,
        )
    )
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=102,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=11945,
            is_primary=True,
        )
    )
    db_session.commit()
    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("79162223355")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 11945
    assert r.matched_contact_id == 102
    assert len(r.candidates) == 2
    assert "наибольшим номером" in r.match_reason


def test_match_crm_ten_digit_phone_vs_call_eleven_digit(db_session):
    _seed_deal(db_session, 8001)
    _seed_contact_phone(db_session, 1485, "3477220403")
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=1485,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=8001,
            is_primary=True,
        )
    )
    db_session.commit()

    m = CallResultMatcher(db_session, PORTAL)
    m.build_indexes()
    r = m.match_row("73477220403")
    assert r.match_status == "matched"
    assert r.matched_deal_id == 8001
    assert r.matched_contact_id == 1485
