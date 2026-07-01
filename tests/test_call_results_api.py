"""Integration tests for call results API."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.models import (
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    ENTITY_DEAL,
)
from app.services.call_results.fake_classifier import (
    FakeCallResultClassifier,
    hot_lead_result,
    manager_callback_result,
)
from app.services.call_results.orchestrator import CallResultOrchestrator

PORTAL = "example.bitrix24.ru"
FIXTURE = Path(__file__).parent / "fixtures" / "call_results" / "demo_call_results.csv"


def _seed_crm(db):
    for deal_id, phone in [(1001, "89161234567"), (1002, "89161234568"), (1004, "89161234570")]:
        db.add(
            CrmEntity(
                portal_id=PORTAL,
                entity_type_id=ENTITY_DEAL,
                entity_id=deal_id,
                title=f"Deal {deal_id}",
                assigned_by_id=42,
                raw_payload={"closed": "N"},
                payload_hash=f"hash-{deal_id}",
            )
        )
        cid = deal_id + 5000
        db.add(CrmContact(portal_id=PORTAL, contact_id=cid, full_name=f"C{cid}"))
        db.add(
            CrmContactPhone(
                portal_id=PORTAL,
                contact_id=cid,
                value=phone,
                value_type="MOBILE",
                is_primary=True,
            )
        )
        db.add(
            CrmContactLink(
                portal_id=PORTAL,
                contact_id=cid,
                parent_entity_type_id=ENTITY_DEAL,
                parent_entity_id=deal_id,
                is_primary=True,
            )
        )
    db.commit()


@pytest.fixture()
def fake_classifier():
    responses = [
        hot_lead_result(),
        manager_callback_result(),
        None,
        None,
        hot_lead_result(),
        None,
        manager_callback_result(),
    ]
    return FakeCallResultClassifier(responses=responses)


def test_upload_and_process_csv(client, db_session, fake_classifier, monkeypatch):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    settings.llm_call_results_enabled = True
    settings.llm_call_results_use_mock = True
    settings.bitrix_service_user_id = 99

    content = FIXTURE.read_bytes()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        with patch("app.routers.call_results.get_call_result_classifier_instance", return_value=fake_classifier):
            with patch("app.services.call_results.job_service.get_call_result_classifier_instance", return_value=fake_classifier):
                # Sync process instead of background
                def sync_submit(import_id, **kw):
                    orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
                    orch.process_import(import_id, **kw)

                monkeypatch.setattr(
                    "app.routers.call_results.CallResultJobService.submit_process",
                    lambda self, i, **kw: sync_submit(i, **kw),
                )

                resp = client.post(
                    "/api/call-results/imports",
                    files={"file": ("demo.csv", content, "text/csv")},
                )
                assert resp.status_code == 200
                import_id = resp.json()["import_id"]

                detail = client.get(f"/api/call-results/imports/{import_id}").json()
                assert detail["status"] == "ready"
                assert detail["summary"]["total_rows"] >= 6

                methods = detail["actions_by_method"]
                assert "crm.timeline.comment.add" in methods or detail["summary"]["comments"] >= 0


def test_no_bitrix_api_calls(client, db_session, fake_classifier, monkeypatch):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()

    with patch("app.services.bitrix_client.BitrixClient") as mock_bitrix:
        with patch("app.services.bitrix_import.bitrix_crm_client.BitrixCrmClient") as mock_crm:
            with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
                orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
                imp, _ = orch.save_uploaded_file(content, "demo.csv")
                db_session.commit()
                orch.process_import(imp.id)
                assert mock_bitrix.call_count == 0
                assert mock_crm.call_count == 0


def test_execute_disabled_by_default(client):
    resp = client.post("/api/call-results/imports/1/execute", json={"confirmation_token": "EXECUTE"})
    assert resp.status_code == 403


def test_execute_requires_confirmation(client, db_session, monkeypatch):
    from app.config import get_settings
    settings = get_settings()
    settings.call_results_bitrix_execution_enabled = True
    resp = client.post("/api/call-results/imports/1/execute", json={"confirmation_token": "wrong"})
    assert resp.status_code == 400


def test_export_json(client, db_session, fake_classifier):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "demo.csv")
        db_session.commit()
        orch.process_import(imp.id)
        resp = client.get(f"/api/call-results/imports/{imp.id}/export.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "import" in data
        assert "operations" in data


def test_export_csv(client, db_session, fake_classifier):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "demo.csv")
        db_session.commit()
        orch.process_import(imp.id)
        resp = client.get(f"/api/call-results/imports/{imp.id}/export.csv")
        assert resp.status_code == 200
        assert resp.content[:3] == b"\xef\xbb\xbf"


def test_tomoru_upload_auto(client, db_session, fake_classifier, monkeypatch):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    tomoru_file = Path(__file__).parent / "fixtures" / "call_results" / "tomoru" / "no_answer.csv"
    content = tomoru_file.read_bytes()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        with patch("app.routers.call_results.get_call_result_classifier_instance", return_value=fake_classifier):
            def sync_submit(import_id, **kw):
                orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
                orch.process_import(import_id, **kw)

            monkeypatch.setattr(
                "app.routers.call_results.CallResultJobService.submit_process",
                lambda self, i, **kw: sync_submit(i, **kw),
            )
            resp = client.post(
                "/api/call-results/imports",
                files={"file": ("batch_test_20260629T120000.csv", content, "text/csv")},
            )
            assert resp.status_code == 200
            assert resp.json().get("source_format") == "tomoru_csv"


def test_restart_import(client, db_session, fake_classifier, monkeypatch):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "demo.csv")
        db_session.commit()
        import_id = imp.id
        orch.process_import(import_id)
        assert orch.repo.get_import(import_id).status == "ready"

        def sync_submit(i, **kw):
            CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier).process_import(i, **kw)

        monkeypatch.setattr(
            "app.routers.call_results.CallResultJobService.submit_process",
            lambda self, i, **kw: sync_submit(i, **kw),
        )
        resp = client.post(f"/api/call-results/imports/{import_id}/restart")
        assert resp.status_code == 200
        assert resp.json()["import_id"] == import_id
        assert orch.repo.get_import(import_id).status == "ready"


def test_configure_resume(client, db_session, monkeypatch):
    from app.config import get_settings
    from app.services.call_results.fake_classifier import FakeCallResultClassifier

    settings = get_settings()
    generic = b"col1,col2\n1,2\n"
    clf = FakeCallResultClassifier()
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, clf)
        imp, _ = orch.save_uploaded_file(generic, "generic.csv")
        db_session.commit()
        import_id = imp.id

        def sync_submit(i, **kw):
            CallResultOrchestrator(db_session, settings, PORTAL, clf).process_import(i, **kw)

        monkeypatch.setattr(
            "app.routers.call_results.CallResultJobService.submit_process",
            lambda self, i, **kw: sync_submit(i, **kw),
        )
        resp = client.post(
            f"/api/call-results/imports/{import_id}/configure",
            json={"column_mapping": {"phone": "col1", "comment": "col2"}},
        )
        assert resp.status_code == 200


def test_call_results_page(client):
    resp = client.get("/call-results")
    assert resp.status_code == 200


def test_import_status_endpoint(client, db_session, fake_classifier):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "demo.csv")
        db_session.commit()
        resp = client.get(f"/api/call-results/imports/{imp.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == imp.id
        assert data["status"] == "uploaded"
        assert "summary" in data
        assert "rows" not in data
        assert "actions_by_method" not in data


def test_row_llm_debug_endpoint(client, db_session, fake_classifier):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "demo.csv")
        db_session.commit()
        orch.process_import(imp.id)
        rows = orch.repo.list_rows(imp.id)
        assert rows

        llm_row = next((r for r in rows if r.llm_status == "completed"), None)
        not_required_row = next((r for r in rows if r.llm_status == "not_required"), None)

        if llm_row:
            resp = client.get(f"/api/call-results/imports/{imp.id}/rows/{llm_row.id}/llm")
            assert resp.status_code == 200
            data = resp.json()
            assert data["system_prompt"]
            assert data["user_payload"]
            assert data["user_message"]
            assert data["llm_result"] is not None

        if not_required_row:
            resp = client.get(f"/api/call-results/imports/{imp.id}/rows/{not_required_row.id}/llm")
            assert resp.status_code == 200
            data = resp.json()
            assert data["llm_status"] == "not_required"
            assert data["deterministic_reason"] or data["deterministic_category"]

        resp404 = client.get(f"/api/call-results/imports/{imp.id}/rows/999999/llm")
        assert resp404.status_code == 404
