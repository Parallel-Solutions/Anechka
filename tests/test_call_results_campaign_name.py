"""Campaign name extraction and presentation for call-result imports."""

from __future__ import annotations

from unittest.mock import patch

from app.config import get_settings
from app.models import CallResultImport
from app.services.call_results.fake_classifier import FakeCallResultClassifier
from app.services.call_results.orchestrator import CallResultOrchestrator, _select_campaign_name

PORTAL = "example.bitrix24.ru"


def test_select_campaign_name_uses_most_frequent_then_first():
    assert _select_campaign_name(["Север", "Юг", "Север"]) == ("Север", True)
    assert _select_campaign_name(["Первая", "Вторая"]) == ("Первая", True)
    assert _select_campaign_name([]) == (None, False)


def test_generic_import_saves_campaign_name(db_session):
    content = (
        "phone,campaign_name,result\n"
        "79991234567,Москва и область,No Answer\n"
        "79991234568,Москва и область,No Answer\n"
    ).encode("utf-8")
    orchestrator = CallResultOrchestrator(
        db_session,
        get_settings(),
        PORTAL,
        FakeCallResultClassifier([]),
    )

    imp, duplicate = orchestrator.save_uploaded_file(content, "campaign.csv")
    assert duplicate is None
    db_session.commit()
    orchestrator.process_import(imp.id)

    db_session.refresh(imp)
    assert imp.campaign_name == "Москва и область"


def test_campaign_name_is_returned_by_api_and_rendered_in_list(client, db_session):
    imp = CallResultImport(
        portal_id=PORTAL,
        original_filename="batch_123.csv",
        campaign_name="Оренбург",
        storage_key="batch_123.csv",
        file_sha256="campaign-name-test",
        file_size=100,
        status="ready",
    )
    db_session.add(imp)
    db_session.commit()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        detail = client.get(f"/api/call-results/imports/{imp.id}")
        page = client.get("/call-results")

    assert detail.status_code == 200
    assert detail.json()["campaign_name"] == "Оренбург"
    assert page.status_code == 200
    assert "Оренбург" in page.text
    assert "batch_123.csv" in page.text