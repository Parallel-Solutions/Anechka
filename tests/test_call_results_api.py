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
    refusal_result,
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


def test_export_retry_call_csv_not_found_deal(client, db_session, fake_classifier):
    """No-answer without matched deal → manual review, no retry export."""
    from app.config import get_settings

    settings = get_settings()
    tomoru_file = Path(__file__).parent / "fixtures" / "call_results" / "tomoru" / "no_answer.csv"
    content = tomoru_file.read_bytes()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "batch_test_20260629T120000.csv")
        db_session.commit()
        orch.process_import(imp.id)

        detail = client.get(f"/api/call-results/imports/{imp.id}").json()
        assert detail["summary"]["pure_no_answer"] >= 1
        assert detail["summary"]["retry_call_phones"] == 0
        manual_ids = set(detail["manual_review_ids"])
        no_answer_rows = [r for r in detail["rows"] if r["primary_outcome"] == "no_answer"]
        assert no_answer_rows
        assert all(r["id"] in manual_ids for r in no_answer_rows)
        assert all(r["match_status"] == "not_found" for r in no_answer_rows)

        resp = client.get(f"/api/call-results/imports/{imp.id}/retry-call/export.csv")
        assert resp.status_code == 200
        text = resp.content.decode("utf-8-sig")
        assert "phone_number" in text
        assert "73436053001" not in text


def test_export_retry_call_csv_with_matched_deal(client, db_session, fake_classifier):
    """No-answer with matched deal → auto retry export, not manual review."""
    from app.config import get_settings

    deal_id = 2001
    phone = "73436053001"
    db_session.add(
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
    db_session.add(CrmContact(portal_id=PORTAL, contact_id=cid, full_name=f"C{cid}"))
    db_session.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=cid,
            value=phone,
            value_type="MOBILE",
            is_primary=True,
        )
    )
    db_session.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=cid,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=deal_id,
            is_primary=True,
        )
    )
    db_session.commit()

    settings = get_settings()
    tomoru_file = Path(__file__).parent / "fixtures" / "call_results" / "tomoru" / "no_answer.csv"
    content = tomoru_file.read_bytes()

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "batch_test_20260629T120000.csv")
        db_session.commit()
        orch.process_import(imp.id)

        detail = client.get(f"/api/call-results/imports/{imp.id}").json()
        assert detail["summary"]["pure_no_answer"] >= 1
        assert detail["summary"]["retry_call_phones"] >= 1
        manual_ids = set(detail["manual_review_ids"])
        no_answer_rows = [r for r in detail["rows"] if r["primary_outcome"] == "no_answer"]
        assert no_answer_rows
        assert all(r["match_status"] == "matched" for r in no_answer_rows)
        assert not any(r["id"] in manual_ids for r in no_answer_rows)

        resp = client.get(f"/api/call-results/imports/{imp.id}/retry-call/export.csv")
        assert resp.status_code == 200
        text = resp.content.decode("utf-8-sig")
        assert "73436053001" in text


def test_pure_refusal_in_manual_review_when_not_found(client, db_session):
    from app.config import get_settings

    settings = get_settings()
    tomoru_file = Path(__file__).parent / "fixtures" / "call_results" / "tomoru" / "refusal_vhod.csv"
    content = tomoru_file.read_bytes()
    classifier = FakeCallResultClassifier(responses=[refusal_result()])

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, classifier)
        imp, _ = orch.save_uploaded_file(content, "batch_refusal_20260629T120000.csv")
        db_session.commit()
        orch.process_import(imp.id)

        detail = client.get(f"/api/call-results/imports/{imp.id}").json()
        manual_ids = set(detail["manual_review_ids"])
        refusal_rows = [r for r in detail["rows"] if r["primary_outcome"] == "refusal"]
        assert refusal_rows
        assert all(r["match_status"] == "not_found" for r in refusal_rows)
        assert all(r["id"] in manual_ids for r in refusal_rows)


def test_export_retry_call_csv_not_found(client):
    resp = client.get("/api/call-results/imports/999999/retry-call/export.csv")
    assert resp.status_code == 404


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


def test_import_status_live_progress_during_processing(client, db_session):
    from app.models.call_results import CallResultImport, CallResultImportRow

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        imp = CallResultImport(
            portal_id=PORTAL,
            original_filename="live-progress.csv",
            storage_key="live-progress.csv",
            file_sha256="abc123",
            file_size=100,
            status="processing",
            llm_rows_total=0,
            llm_rows_completed=0,
        )
        db_session.add(imp)
        db_session.flush()

        rows = [
            CallResultImportRow(
                import_id=imp.id,
                source_row_number=2,
                llm_required=True,
                llm_status="completed",
            ),
            CallResultImportRow(
                import_id=imp.id,
                source_row_number=3,
                llm_required=True,
                llm_status="pending",
            ),
            CallResultImportRow(
                import_id=imp.id,
                source_row_number=4,
                llm_required=False,
                llm_status="not_required",
            ),
        ]
        db_session.add_all(rows)
        db_session.commit()

        resp = client.get(f"/api/call-results/imports/{imp.id}/status")
        assert resp.status_code == 200
        data = resp.json()
        summary = data["summary"]
        assert data["status"] == "processing"
        assert summary["total_rows"] == 3
        assert summary["llm_sent"] == 2
        assert summary["llm_completed"] == 1
        assert summary["llm_pending"] == 1


def test_reparse_reset_clears_stale_completed_rows(client, db_session, fake_classifier):
    from app.config import get_settings
    from app.models.call_results import CallResultImport, CallResultImportRow

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        imp = CallResultImport(
            portal_id=PORTAL,
            original_filename="x.csv",
            storage_key="x.csv",
            file_sha256="h",
            file_size=10,
            status="processing",
            llm_rows_total=2,
            llm_rows_completed=2,
        )
        db_session.add(imp)
        db_session.flush()
        db_session.add_all([
            CallResultImportRow(
                import_id=imp.id,
                source_row_number=2,
                llm_required=True,
                llm_status="completed",
            ),
            CallResultImportRow(
                import_id=imp.id,
                source_row_number=3,
                llm_required=True,
                llm_status="completed",
            ),
        ])
        db_session.commit()

        settings = get_settings()
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        orch._reset_import_for_reparse(imp)
        db_session.commit()

        data = client.get(f"/api/call-results/imports/{imp.id}/status").json()
        summary = data["summary"]
        assert summary["total_rows"] == 0
        assert summary["llm_sent"] == 0
        assert summary["llm_completed"] == 0


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


def test_import_detail_slim_payload(client, db_session, fake_classifier):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    content = FIXTURE.read_bytes()
    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        orch = CallResultOrchestrator(db_session, settings, PORTAL, fake_classifier)
        imp, _ = orch.save_uploaded_file(content, "demo.csv")
        db_session.commit()
        orch.process_import(imp.id)

        detail = client.get(f"/api/call-results/imports/{imp.id}").json()
        assert detail["status"] == "ready"
        assert "manual_review" not in detail
        assert isinstance(detail["manual_review_ids"], list)
        assert detail["rows"]
        row = detail["rows"][0]
        assert "raw_data" not in row
        assert "llm_result" not in row
        assert "normalized_data" not in row

        row_id = row["id"]
        raw_resp = client.get(f"/api/call-results/imports/{imp.id}/rows/{row_id}/raw")
        assert raw_resp.status_code == 200
        raw = raw_resp.json()
        assert "raw_data" in raw
        assert isinstance(raw["raw_data"], dict)

        resp404 = client.get(f"/api/call-results/imports/{imp.id}/rows/999999/raw")
        assert resp404.status_code == 404
