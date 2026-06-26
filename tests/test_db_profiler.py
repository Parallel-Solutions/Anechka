"""Read-only portal profiler: fill-rate, FM-vs-PHONE, FK link share, UF fields."""

from __future__ import annotations

from app.models import (
    CrmEntity,
    CrmFieldDefinition,
    CrmFieldSemantic,
    ENTITY_CONTACT,
    ENTITY_DEAL,
)
from app.services.export_plan.catalog import FieldCatalog
from app.services.intelligent_export.db_profiler import PortalProfiler

PORTAL = "test.bitrix24.ru"


def _contact(db, eid, payload):
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_CONTACT,
            entity_id=eid,
            title=f"Contact {eid}",
            payload_hash=f"c{eid}",
            raw_payload=payload,
        )
    )


def _deal(db, eid, payload):
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=eid,
            title=f"Deal {eid}",
            payload_hash=f"d{eid}",
            raw_payload=payload,
        )
    )


def _seed_uf_field(db):
    fd = CrmFieldDefinition(
        portal_id=PORTAL,
        entity_type_id=ENTITY_DEAL,
        original_field_name="UF_CRM_CITY",
        upper_name="UF_CRM_CITY",
        field_type="string",
        is_custom=True,
        is_active=True,
        definition_hash="h",
    )
    db.add(fd)
    db.flush()
    db.add(CrmFieldSemantic(field_definition_id=fd.id, display_name="Город клиента"))


def _profile(db):
    catalog = FieldCatalog.load(db, PORTAL)
    return PortalProfiler(db, PORTAL, catalog).profile(sample_cap=1000), catalog


def test_phone_fm_detection_and_case_insensitive(db_session):
    # UPPER-cased payload
    _contact(
        db_session,
        1,
        {
            "id": 1,
            "PHONE": [{"VALUE": "8 916 111 22 33", "VALUE_TYPE": "WORK"}],
            "FM": [{"value": "a@b.ru", "typeId": "EMAIL"}, {"value": "89161112233", "typeId": "PHONE"}],
        },
    )
    # lower camelCase payload (real portal convention)
    _contact(
        db_session,
        2,
        {
            "id": 2,
            "phone": [{"value": "8 901 000 00 00"}],
            "fm": [{"value": "c@d.ru", "typeId": "EMAIL"}],
        },
    )
    # no phone at all
    _contact(db_session, 3, {"id": 3})
    db_session.commit()

    profile, _ = _profile(db_session)
    contact = profile.entities[ENTITY_CONTACT]

    assert contact.total_seen == 3
    assert contact.multifield["PHONE"]["present"] is True
    assert contact.multifield["PHONE"]["count"] == 2  # contacts 1 and 2
    assert abs(contact.multifield["PHONE"]["fill_rate"] - (2 / 3)) < 1e-6
    assert contact.multifield["FM"]["present"] is True
    # phones start with 8
    assert contact.phone_format is not None
    assert contact.phone_format["starts_with_8"] == 1.0


def test_fk_link_share(db_session):
    _deal(db_session, 1, {"id": 1, "TITLE": "D1", "contactId": 5})
    _deal(db_session, 2, {"id": 2, "TITLE": "D2", "contactId": 0})
    _deal(db_session, 3, {"id": 3, "TITLE": "D3"})
    _deal(db_session, 4, {"id": 4, "TITLE": "D4", "contactId": 0})
    db_session.commit()

    profile, _ = _profile(db_session)
    deal = profile.entities[ENTITY_DEAL]

    assert deal.total_seen == 4
    contact_link = deal.fk_link_shares["contactId"]
    assert contact_link["linked"] == 1
    assert contact_link["share"] == 0.25


def test_fill_rate_and_uf_collection(db_session):
    _seed_uf_field(db_session)
    _deal(db_session, 1, {"id": 1, "TITLE": "D1", "UF_CRM_CITY": "Москва"})
    _deal(db_session, 2, {"id": 2, "TITLE": "D2"})
    _deal(db_session, 3, {"id": 3, "TITLE": "D3"})
    _deal(db_session, 4, {"id": 4, "TITLE": "D4"})
    db_session.commit()

    profile, catalog = _profile(db_session)
    deal = profile.entities[ENTITY_DEAL]

    # Only payload-backed (jsonb) fields are scanned; the denorm column TITLE is not.
    assert "TITLE" not in deal.fill_rates
    assert deal.fill_rates["UF_CRM_CITY"] == 0.25

    uf = [u for u in profile.uf_fields if u[1] == "UF_CRM_CITY"]
    assert uf, "UF field must be collected"
    entity_type_id, code, display, informative = uf[0]
    assert entity_type_id == ENTITY_DEAL
    assert display == "Город клиента"
    assert informative is True


def test_profiler_is_read_only(db_session):
    _deal(db_session, 1, {"id": 1, "TITLE": "D1"})
    db_session.commit()
    before = db_session.query(CrmEntity).count()
    _profile(db_session)
    assert db_session.query(CrmEntity).count() == before
