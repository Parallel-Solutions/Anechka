"""Tests for export phone registry."""

from app.models import ExportJob, ExportPhoneEntry
from app.services.export_phone_registry import ExportPhoneRegistry, ExportPhoneRow
from app.services.phone_service import phone_hash

PORTAL = "example.bitrix24.ru"


def test_save_and_lookup(db_session):
    job = ExportJob(mode="region_lpr", status="completed", parameters_json="{}")
    db_session.add(job)
    db_session.commit()

    registry = ExportPhoneRegistry(db_session)
    saved = registry.save_entries(
        PORTAL,
        job.id,
        "region_lpr",
        [
            ExportPhoneRow(phone="89161112233", deal_id=1001, contact_id=50),
            ExportPhoneRow(phone="+7 916 111 22 33", deal_id=1001, contact_id=50),
        ],
    )
    assert saved == 1
    db_session.commit()

    found = registry.lookup(PORTAL, "79161112233")
    assert len(found) == 1
    assert found[0].deal_id == 1001
    assert found[0].contact_id == 50


def test_lookup_empty(db_session):
    registry = ExportPhoneRegistry(db_session)
    assert registry.lookup(PORTAL, "79990001122") == []


def test_dedup_within_export_job(db_session):
    job = ExportJob(mode="region_lpr", status="completed", parameters_json="{}")
    db_session.add(job)
    db_session.commit()

    registry = ExportPhoneRegistry(db_session)
    saved = registry.save_entries(
        PORTAL,
        job.id,
        "region_lpr",
        [
            {"phone": "79161234567", "deal_id": 1, "contact_id": 10},
            {"phone": "79161234567", "deal_id": 2, "contact_id": 20},
        ],
    )
    assert saved == 1
    db_session.commit()

    count = db_session.query(ExportPhoneEntry).filter(ExportPhoneEntry.export_job_id == job.id).count()
    assert count == 1


def test_lookup_legacy_ten_digit_normalized_phone(db_session):
    job = ExportJob(mode="region_lpr", status="completed", parameters_json="{}")
    db_session.add(job)
    db_session.commit()

    db_session.add(
        ExportPhoneEntry(
            portal_id=PORTAL,
            export_job_id=job.id,
            phone_hash=phone_hash("3477220403"),
            normalized_phone="3477220403",
            deal_id=9001,
            contact_id=1485,
            export_mode="region_lpr",
        )
    )
    db_session.commit()

    found = ExportPhoneRegistry(db_session).lookup(PORTAL, "73477220403")
    assert len(found) == 1
    assert found[0].deal_id == 9001
