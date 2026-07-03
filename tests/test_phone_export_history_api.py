"""API tests for phone export history."""

from unittest.mock import patch

from app.models import ExportJob
from app.services.export_phone_registry import ExportPhoneRegistry, ExportPhoneRow

PORTAL = "example.bitrix24.ru"


def test_phone_export_history_returns_items(client, db_session):
    job = ExportJob(
        mode="region_lpr",
        status="completed",
        parameters_json='{"category_id": 15}',
    )
    db_session.add(job)
    db_session.commit()

    ExportPhoneRegistry(db_session).save_entries(
        PORTAL,
        job.id,
        "region_lpr",
        [ExportPhoneRow(phone="89161234567", deal_id=2001, contact_id=77)],
    )
    db_session.commit()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        resp = client.get("/api/phones/export-history", params={"phone": "+7 916 123-45-67"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["normalized_phone"] == "79161234567"
    assert len(data["items"]) == 1
    assert data["items"][0]["export_job_id"] == job.id
    assert data["items"][0]["deal_id"] == 2001
    assert data["items"][0]["contact_id"] == 77


def test_phone_export_history_empty(client, db_session):
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        resp = client.get("/api/phones/export-history", params={"phone": "79990001122"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []


def test_phone_export_history_invalid_phone(client, db_session):
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        resp = client.get("/api/phones/export-history", params={"phone": "abc"})

    assert resp.status_code == 400
