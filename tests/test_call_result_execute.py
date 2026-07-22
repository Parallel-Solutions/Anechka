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
    callback_later_result,
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
CALLBACK_ROW_CSV = (
    "phone,comment,category,transcript,called_at,deal_id,call_id\n"
    '89161234568,"Call back tomorrow",robot_callback,"Call me tomorrow at 3pm",2026-06-29T11:00:00+03:00,1002,call-002\n'
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


def test_execute_positive_task(db_session):
    from app.config import get_settings

    imp, row, settings, orch = _process_csv(db_session, HOT_ROW_CSV)
    task_actions = [
        a for a in orch.repo.list_actions(imp.id)
        if a.method == "tasks.task.add" and a.import_row_id == row.id
    ]
    assert len(task_actions) == 1
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id, responsible_user_id=999)
    assert stats["succeeded"] >= 1
    assert len(gw.tasks) == 1
    assert len(gw.comments) == 0
    task_actions = [a for a in svc.repo.list_actions(imp.id) if a.method == "tasks.task.add"]
    assert task_actions[0].execution_status == "succeeded"
    assert gw.tasks[0]["fields"]["RESPONSIBLE_ID"] == 42
    assert gw.tasks[0]["fields"]["CREATED_BY"] == 999
    assert task_actions[0].response_payload.get("bitrix_task_url")
    assert task_actions[0].response_payload.get("bitrix_task_link_source") == "api_link"
    assert "/user/42/tasks/task/view/1/" in task_actions[0].response_payload.get("bitrix_task_url")
    assert task_actions[0].response_payload.get("status") == "created"


def test_execute_todo_uses_deal_assignee_not_operator(db_session):
    from app.config import get_settings
    from app.models import BitrixPreparedAction

    imp, row, settings, orch = _process_csv(db_session, HOT_ROW_CSV)
    todo_actions = [
        a for a in orch.repo.list_actions(imp.id)
        if a.method == "crm.activity.todo.add" and a.import_row_id == row.id
    ]
    if not todo_actions:
        db_session.add(
            BitrixPreparedAction(
                import_id=imp.id,
                import_row_id=row.id,
                action_group_id="test-group",
                method="crm.activity.todo.add",
                action_type="crm_todo",
                operation_type="bitrix_add_todo",
                payload={
                    "ownerTypeId": 2,
                    "ownerId": row.matched_deal_id,
                    "title": "Test todo",
                    "description": "Test",
                    "pingOffsets": [0, 15],
                    "responsibleId": 999,
                },
                human_summary="CRM-дело: тест",
                validation_status="valid",
                is_enabled=True,
                idempotency_key="test-todo-override",
            )
        )
        db_session.commit()
    else:
        todo_actions[0].payload = dict(todo_actions[0].payload or {})
        todo_actions[0].payload["responsibleId"] = 999
        db_session.commit()

    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id, responsible_user_id=999)
    assert stats["succeeded"] >= 1
    assert len(gw.todos) == 1
    assert gw.todos[0]["responsibleId"] == 42


def test_execute_todo_uses_deal_assignee_not_service_user(db_session):
    from app.config import get_settings
    from app.models import BitrixPreparedAction

    imp, row, settings, orch = _process_csv(db_session, HOT_ROW_CSV)
    settings.bitrix_service_user_id = 77
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=row.id,
            action_group_id="test-todo-fallback",
            method="crm.activity.todo.add",
            action_type="crm_todo",
            operation_type="bitrix_add_todo",
            payload={
                "ownerTypeId": 2,
                "ownerId": row.matched_deal_id,
                "title": "Test todo",
                "description": "Test",
                "pingOffsets": [0, 15],
            },
            human_summary="CRM-дело: тест",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="test-todo-fallback",
        )
    )
    db_session.commit()
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id)
    assert stats["succeeded"] >= 1
    assert gw.todos[0]["responsibleId"] == 42


def test_execute_idempotent_skip_succeeded(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    svc.execute_import(imp.id, responsible_user_id=42)
    stats = svc.execute_import(imp.id, responsible_user_id=42)
    assert stats["skipped"] >= 1
    assert len(gw.tasks) == 1
    assert len(gw.comments) == 0


def test_process_callback_later_adds_outcome_comment(db_session):
    from app.config import get_settings
    from app.services.call_results.row_disposition import get_row_disposition

    imp, row, settings, orch = _process_csv(
        db_session,
        CALLBACK_ROW_CSV,
        FakeCallResultClassifier([callback_later_result()]),
    )
    actions = orch.repo.list_actions(imp.id)
    row_actions = [a for a in actions if a.import_row_id == row.id]
    comment_actions = [a for a in row_actions if a.method == "crm.timeline.comment.add"]
    assert comment_actions
    assert get_row_disposition(row, row_actions) == "manual_call"


def test_execute_callback_later_comment(db_session):
    from app.config import get_settings

    imp, row, settings, orch = _process_csv(
        db_session,
        CALLBACK_ROW_CSV,
        FakeCallResultClassifier([callback_later_result()]),
    )
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_row(row, imp)
    assert stats["succeeded"] >= 2
    assert len(gw.comments) == 1
    retry_actions = [
        a for a in svc.repo.list_actions(imp.id)
        if a.import_row_id == row.id and a.method == "retry_queue.add"
    ]
    assert retry_actions
    assert retry_actions[0].execution_status == "succeeded"


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
    assert len(gw.tasks) == 0


def test_execute_task_without_responsible_fails(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    deal = db_session.query(CrmEntity).filter(
        CrmEntity.portal_id == PORTAL,
        CrmEntity.entity_id == row.matched_deal_id,
    ).one()
    deal.assigned_by_id = None
    settings.bitrix_service_user_id = 0
    db_session.commit()
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id)
    task_actions = [a for a in svc.repo.list_actions(imp.id) if a.method == "tasks.task.add"]
    assert stats["failed"] >= 1
    assert task_actions[0].execution_status == "failed"
    assert "ответственного" in (task_actions[0].last_error or "").lower()
    assert len(gw.tasks) == 0


def test_execute_task_creator_fallback_service_user(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.bitrix_service_user_id = 77
    db_session.commit()
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    svc.execute_import(imp.id)
    assert gw.tasks[0]["fields"]["RESPONSIBLE_ID"] == 42
    assert gw.tasks[0]["fields"]["CREATED_BY"] == 77


def test_execute_task_fails_without_deal_id(db_session):
    from app.config import get_settings
    from app.models import BitrixPreparedAction

    imp, row, settings, orch = _process_csv(db_session, HOT_ROW_CSV)
    row.matched_deal_id = None
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=row.id,
            action_group_id="orphan-task",
            method="tasks.task.add",
            action_type="task",
            operation_type="bitrix_add_task",
            payload={"fields": {"TITLE": "T", "UF_CRM_TASK": ["D_0"]}},
            human_summary="task",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="orphan-task",
        )
    )
    db_session.commit()
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_row(row, imp)
    task = [a for a in orch.repo.list_actions(imp.id) if a.method == "tasks.task.add"][-1]
    assert task.execution_status == "failed"
    assert "сделки" in (task.last_error or "").lower()
    assert len(gw.tasks) == 0


def test_execute_task_responsible_mismatch_uses_api_link(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.bitrix_service_user_id = 42
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway(verify_mismatch=True)
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id)
    task_actions = [a for a in svc.repo.list_actions(imp.id) if a.method == "tasks.task.add"]
    assert stats["succeeded"] >= 1
    assert task_actions[0].execution_status == "succeeded"
    payload = task_actions[0].response_payload
    assert payload.get("bitrix_task_link_source") == "api_link"
    assert "/user/43/tasks/task/view/1/" in payload.get("bitrix_task_url", "")
    assert payload.get("responsible_user_id") == 43
    assert payload.get("warning")


def test_execute_task_empty_id_fails(db_session):
    from app.config import get_settings

    imp, row, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
    settings.call_results_bitrix_execution_enabled = True
    gw = FakeBitrixGateway(empty_task_id=True)
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_import(imp.id)
    task_actions = [a for a in svc.repo.list_actions(imp.id) if a.method == "tasks.task.add"]
    assert stats["failed"] >= 1
    assert task_actions[0].execution_status == "failed"
    assert len(gw.tasks) == 0


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
    assert len(gw.tasks) == 0


def test_execute_api_enabled(client, db_session, monkeypatch):
    from app.config import get_settings
    from app.services.auth_service import AuthService

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        imp, _, settings, _ = _process_csv(db_session, HOT_ROW_CSV)
        settings.call_results_bitrix_execution_enabled = True
        user = AuthService(settings, db_session).get_default_ie_user()
        user.crm_user_external_id = 42
        db_session.commit()

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


def test_execute_status_with_row_ids_returns_response_payload(client, db_session, monkeypatch):
    from app.config import get_settings
    from app.services.auth_service import AuthService

    with patch("app.services.auth_service.resolve_portal_id", return_value=PORTAL):
        imp, row, settings, orch = _process_csv(db_session, HOT_ROW_CSV)
        settings.call_results_bitrix_execution_enabled = True
        user = AuthService(settings, db_session).get_default_ie_user()
        user.crm_user_external_id = 42
        db_session.commit()

        gw = FakeBitrixGateway()
        CrmActionService(db_session, settings, PORTAL, gateway=gw).execute_import(
            imp.id, row_ids=[row.id], responsible_user_id=42,
        )

        resp = client.get(
            f"/api/call-results/imports/{imp.id}/execute/status",
            params={"row_ids": str(row.id)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["execute_status"] in ("completed", "partial")
        assert data["succeeded"] >= 1
        assert data["items"] is not None
        assert len(data["items"]) >= 1
        succeeded = [i for i in data["items"] if i["execution_status"] == "succeeded"]
        assert succeeded
        assert succeeded[0]["response_payload"] is not None
        assert succeeded[0]["source_row_number"] == row.source_row_number
