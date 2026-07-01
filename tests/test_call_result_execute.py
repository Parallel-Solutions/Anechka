"""Execute flow tests for CrmActionService."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.models import (
    CrmContact,
    CrmContactLink,
    CrmContactPhone,
    CrmEntity,
    ENTITY_DEAL,
)
from app.services.call_results.crm_action_service import CrmActionService
from app.services.call_results.fake_bitrix_gateway import FakeBitrixGateway
from app.services.call_results.fake_classifier import (
    FakeCallResultClassifier,
    hot_lead_result,
    refusal_result,
)
from app.services.call_results.orchestrator import CallResultOrchestrator

PORTAL = "example.bitrix24.ru"

HOT_ROW_CSV = (
    "phone,comment,category,transcript,called_at,deal_id,call_id\n"
    '89161234567,"Need KP",hot_lead,"Client confirmed",2026-06-29T10:00:00+03:00,1001,call-001\n'
).encode("utf-8")
REFUSAL_ROW_CSV = (
    "phone,comment,category,transcript,called_at,deal_id,call_id\n"
    '89161234570,"Do not call again",Do Not Call,"Refused",2026-06-29T13:00:00+03:00,1004,call-004\n'
).encode("utf-8")


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


def _process_csv(db_session, content: bytes, classifier: FakeCallResultClassifier | None = None):
    from app.config import get_settings

    _seed_crm(db_session)
    settings = get_settings()
    clf = classifier or FakeCallResultClassifier([hot_lead_result()])
    orch = CallResultOrchestrator(db_session, settings, PORTAL, clf)
    imp, _ = orch.save_uploaded_file(content, "demo.csv")
    db_session.commit()
    orch.process_import(imp.id)
    row = orch.repo.list_rows(imp.id)[0]
    return imp, row, settings, orch


def test_execute_disabled_raises(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.call_results_bitrix_execution_enabled = False
    svc = CrmActionService(db_session, settings, PORTAL, gateway=FakeBitrixGateway())
    with pytest.raises(PermissionError):
        svc.execute_import(imp.id)


def test_execute_positive_todo(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id)
    assert stats["succeeded"] >= 1
    assert len(gw.todos) == 1
    todo_actions = [a for a in svc.repo.list_actions(imp.id) if a.method == "crm.activity.todo.add"]
    assert todo_actions[0].execution_status == "succeeded"


def test_execute_idempotent_skip_succeeded(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    svc.execute_import(imp.id)
    stats = svc.execute_import(imp.id)
    assert stats["skipped"] >= 1
    assert len(gw.todos) == 1


def test_execute_refusal_comment(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(
        db_session,
        REFUSAL_ROW_CSV,
        FakeCallResultClassifier([refusal_result()]),
    )
    assert (row.business_signals or {}).get("explicit_refusal") or row.primary_outcome == "refusal"
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_row(row, imp)
    assert stats["succeeded"] >= 1
    assert len(gw.comments) == 1
    assert len(gw.todos) == 0


def test_execute_blocks_manual_review(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    row.needs_manual_review = True
    row.execution_status = "blocked_manual_review"
    db_session.commit()
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id, row_ids=[row.id])
    assert stats["blocked"] == 1
    assert len(gw.todos) == 0


def test_execute_api_enabled(client, db_session, monkeypatch):
    from app.config import get_settings

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        imp, _, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
        settings.call_results_bitrix_execution_enabled = True

        def sync_execute(import_id, **kw):
            gw = FakeBitrixGateway()
            CrmActionService(db_session, settings, PORTAL, gateway=gw).execute_import(import_id, **kw)

        monkeypatch.setattr(
            "app.routers.call_results.CallResultJobService.submit_execute",
            lambda self, i, **kw: sync_execute(i, **kw),
        )
        resp = client.post(
            f"/api/call-results/imports/{imp.id}/execute",
            json={"confirmation_token": "EXECUTE"},
        )
        assert resp.status_code == 200
