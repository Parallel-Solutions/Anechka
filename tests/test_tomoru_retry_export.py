"""Tomoru retry dry-run ZIP tests."""

from __future__ import annotations

import io
import zipfile

from app.config import get_settings
from app.models import CallRetryQueueEntry
from app.services.auth_service import resolve_portal_id


def test_dry_run_zip_route_contains_manifest_and_campaign_csv(client, db_session):
    portal_id = resolve_portal_id(get_settings())
    db_session.add(
        CallRetryQueueEntry(
            portal_id=portal_id,
            phone_normalized="79298695656",
            phone_extension="456",
            callback_text="Перезвонить позже",
            reason="callback_later",
            status="ready",
            search_required=False,
            idempotency_key="dry-run-zip-entry",
            timezone="Europe/Moscow",
        )
    )
    db_session.commit()

    response = client.get("/api/call-results/retry-queue/tomoru-dry-run.zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "manifest.csv" in names
        assert "README.txt" in names
        campaign_csv = next(name for name in names if name.endswith(".csv") and name != "manifest.csv")
        manifest = archive.read("manifest.csv").decode("utf-8-sig")
        contacts = archive.read(campaign_csv).decode("utf-8-sig")
        assert "Europe/Moscow" in manifest
        assert "callback_later" in manifest
        assert "phone_number,phone_extension" in contacts
        assert "79298695656,456" in contacts
