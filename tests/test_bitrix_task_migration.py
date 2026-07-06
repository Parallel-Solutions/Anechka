"""Tests for positive -> tasks.task.add migration and replan."""

from __future__ import annotations

from datetime import datetime, timezone

from app.models import (
    BitrixPreparedAction,
    CallResultImport,
    CallResultImportRow,
    CrmEntity,
    ENTITY_DEAL,
)
from app.services.call_results.action_planner import BitrixActionPlanner, PlannedAction
from app.services.call_results.bitrix_gateway import (
    _parse_task_id_from_add_response,
    verify_created_task,
)
from app.services.call_results.crm_action_service import CrmActionService
from app.services.call_results.fake_bitrix_gateway import FakeBitrixGateway
from app.services.call_results.fake_classifier import FakeCallResultClassifier
from app.services.call_results.llm_schema import CallResultSignals
from app.services.call_results.orchestrator import CallResultOrchestrator
from app.services.call_results.payload_builder import BitrixPayloadBuilder
from app.services.call_results.replan_service import is_positive_row, replan_positive_to_tasks
from app.utils.portal import bitrix_task_url

PORTAL = "bitrix24.parresh.ru"


def _positive_row(**kw):
    defaults = dict(
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        raw_phone="+79161234567",
        normalized_phone="9161234567",
        match_status="matched",
        matched_deal_id=18685,
        matched_contact_id=55,
        primary_outcome="positive",
        business_signals={"positive": True, "summary": "Нужно КП"},
        call_id="call-pos-001",
        execution_status="prepared",
    )
    defaults.update(kw)
    return CallResultImportRow(**defaults)


def _seed_import(db, *, import_id: int = 1) -> CallResultImport:
    imp = CallResultImport(
        id=import_id,
        portal_id=PORTAL,
        original_filename="batch.csv",
        storage_key=f"{PORTAL}/batch.csv",
        file_sha256="abc",
        file_size=100,
        status="ready",
    )
    db.add(imp)
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=18685,
            title="Deal 18685",
            assigned_by_id=42,
            raw_payload={"closed": "N"},
            payload_hash="hash-18685",
        )
    )
    db.flush()
    return imp


def test_positive_plans_tasks_task_add_only():
    actions = BitrixActionPlanner().plan(
        _positive_row(id=1),
        bitrix_deal_id=18685,
        assigned_by_id=42,
        signals=CallResultSignals(positive=True, summary="Нужно КП", confidence=0.9),
        requires_manual=False,
    )
    methods = [a.method for a in actions]
    assert methods == ["tasks.task.add"]
    assert "crm.activity.todo.add" not in methods


def test_parse_task_id_from_result_task_id():
    data = {"result": {"task": {"id": 151934}}}
    assert _parse_task_id_from_add_response(data) == 151934


def test_task_payload_fields():
    row = _positive_row()
    pa = PlannedAction(
        method="tasks.task.add",
        action_type="task",
        operation_type="bitrix_add_task",
        payload={},
        human_summary="",
    )
    payload = BitrixPayloadBuilder().build(
        pa,
        row,
        bitrix_deal_id=18685,
        assigned_by_id=42,
        service_user_id=464,
    )
    assert "fields" in payload
    fields = payload["fields"]
    assert fields["TITLE"]
    assert fields["UF_CRM_TASK"] == ["D_18685"]


def test_bitrix_task_url_format():
    url = bitrix_task_url(PORTAL, 151934, user_id=464)
    assert url == (
        "https://bitrix24.parresh.ru/company/personal/user/464/tasks/task/view/151934/"
    )


def test_verify_created_task_warns_on_creator_mismatch():
    result = verify_created_task(
        {
            "id": 1,
            "RESPONSIBLE_ID": 457,
            "CREATED_BY": 464,
            "UF_CRM_TASK": ["D_18685"],
        },
        task_id=1,
        deal_id=18685,
        responsible_id=457,
    )
    assert result.error is None
    assert result.responsible_user_id == 457
    assert result.warning is not None
    assert "постановщика 464" in result.warning


def test_verify_created_task_no_warning_when_creator_matches():
    result = verify_created_task(
        {
            "id": 1,
            "RESPONSIBLE_ID": 457,
            "CREATED_BY": 457,
            "UF_CRM_TASK": ["D_18685"],
        },
        task_id=1,
        deal_id=18685,
        responsible_id=457,
    )
    assert result.error is None
    assert result.warning is None


def test_succeeded_todo_does_not_block_task_creation(db_session):
    from app.config import get_settings

    imp = _seed_import(db_session)
    row = _positive_row(id=10, import_id=imp.id)
    db_session.add(row)
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=10,
            action_group_id="old-todo",
            method="crm.activity.todo.add",
            action_type="crm_todo",
            operation_type="bitrix_add_todo",
            payload={"title": "Old todo", "ownerId": 18685, "ownerTypeId": 2},
            human_summary="CRM-дело",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="crm.activity.todo.add:18685:src1:bitrix_add_todo",
            execution_status="succeeded",
            external_id="151933",
        )
    )
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=10,
            action_group_id="new-task",
            method="tasks.task.add",
            action_type="task",
            operation_type="bitrix_add_task",
            payload={
                "fields": {
                    "TITLE": "Task",
                    "DESCRIPTION": "Desc",
                    "UF_CRM_TASK": ["D_18685"],
                }
            },
            human_summary="Задача",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="tasks.task.add:18685:src1:bitrix_add_task",
            execution_status="prepared",
        )
    )
    db_session.commit()

    settings = get_settings()
    settings.call_results_bitrix_execution_enabled = True
    settings.bitrix_service_user_id = 42
    gw = FakeBitrixGateway()
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    stats = svc.execute_row(row, imp)
    assert stats["succeeded"] >= 1
    assert len(gw.tasks) == 1
    task_actions = [
        a for a in svc.repo.list_actions(imp.id) if a.method == "tasks.task.add"
    ]
    assert task_actions[0].execution_status == "succeeded"
    assert "/user/42/tasks/task/view/" in task_actions[0].response_payload.get(
        "bitrix_task_url", ""
    )


def test_bitrix_task_error_saved(db_session):
    from app.config import get_settings

    imp = _seed_import(db_session)
    row = _positive_row(id=11, import_id=imp.id)
    db_session.add(row)
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=11,
            action_group_id="fail-task",
            method="tasks.task.add",
            action_type="task",
            operation_type="bitrix_add_task",
            payload={
                "fields": {
                    "TITLE": "Task",
                    "DESCRIPTION": "Desc",
                    "UF_CRM_TASK": ["D_18685"],
                }
            },
            human_summary="Задача",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="tasks.task.add:18685:src2:bitrix_add_task",
            execution_status="prepared",
        )
    )
    db_session.commit()

    settings = get_settings()
    settings.call_results_bitrix_execution_enabled = True
    settings.bitrix_service_user_id = 42
    gw = FakeBitrixGateway(fail_on={"tasks.task.add"})
    svc = CrmActionService(db_session, settings, PORTAL, gateway=gw)
    svc.execute_row(row, imp)
    task = [
        a for a in svc.repo.list_actions(imp.id) if a.method == "tasks.task.add"
    ][0]
    assert task.execution_status == "failed"
    assert task.response_payload.get("status") == "failed"
    assert task.response_payload.get("error_message")


def test_replan_disables_todo_and_adds_task(db_session):
    from app.config import get_settings

    imp = _seed_import(db_session, import_id=99)
    row = _positive_row(id=20, import_id=imp.id)
    db_session.add(row)
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=20,
            action_group_id="todo-pending",
            method="crm.activity.todo.add",
            action_type="crm_todo",
            operation_type="bitrix_add_todo",
            payload={"title": "Todo", "ownerId": 18685, "ownerTypeId": 2},
            human_summary="CRM-дело",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="todo-pending-key",
            execution_status="prepared",
        )
    )
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=20,
            action_group_id="todo-done",
            method="crm.activity.todo.add",
            action_type="crm_todo",
            operation_type="bitrix_add_todo",
            payload={"title": "Done todo", "ownerId": 18685, "ownerTypeId": 2},
            human_summary="CRM-дело done",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="todo-done-key",
            execution_status="succeeded",
            external_id="151933",
        )
    )
    db_session.commit()

    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session, settings, PORTAL, FakeCallResultClassifier([])
    )
    orch.matcher.build_indexes()
    report = replan_positive_to_tasks(orch, imp.id)

    assert report.found == 1
    assert report.replanned == 1
    assert report.todos_disabled == 1

    actions = orch.repo.list_actions(imp.id)
    todos = [a for a in actions if a.method == "crm.activity.todo.add"]
    tasks = [a for a in actions if a.method == "tasks.task.add"]
    assert len(tasks) == 1
    assert tasks[0].execution_status == "prepared"
    assert any(t.execution_status == "succeeded" for t in todos)
    assert any(t.execution_status == "prepared" and not t.is_enabled for t in todos)


def test_replan_skips_when_task_already_succeeded(db_session):
    from app.config import get_settings

    imp = _seed_import(db_session, import_id=100)
    row = _positive_row(id=21, import_id=imp.id)
    db_session.add(row)
    db_session.add(
        BitrixPreparedAction(
            import_id=imp.id,
            import_row_id=21,
            action_group_id="task-done",
            method="tasks.task.add",
            action_type="task",
            operation_type="bitrix_add_task",
            payload={"fields": {"TITLE": "T", "UF_CRM_TASK": ["D_18685"]}},
            human_summary="Задача",
            validation_status="valid",
            is_enabled=True,
            idempotency_key="task-done-key",
            execution_status="succeeded",
            external_id="999",
        )
    )
    db_session.commit()

    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session, settings, PORTAL, FakeCallResultClassifier([])
    )
    report = replan_positive_to_tasks(orch, imp.id)
    assert report.already_had_task == 1
    assert report.replanned == 0
    assert len([a for a in orch.repo.list_actions(imp.id) if a.method == "tasks.task.add"]) == 1


def test_is_positive_row():
    assert is_positive_row(_positive_row())
    assert not is_positive_row(_positive_row(primary_outcome="refusal", business_signals={}))
