"""Unit tests for Bitrix payload builder and validator."""

from datetime import datetime, timezone

from app.models import CallResultImportRow
from app.services.call_results.action_planner import PlannedAction
from app.services.call_results.callback_date_resolver import CallbackDateResolver
from app.services.call_results.payload_builder import BitrixPayloadBuilder
from app.services.call_results.payload_validator import BitrixPayloadValidator


def _row(**kw):
    defaults = dict(
        id=1,
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        raw_phone="+79161234567",
        match_status="matched",
        llm_status="not_required",
        llm_required=False,
        manually_overridden=False,
        llm_input_truncated=False,
        is_duplicate=False,
        needs_manual_review=False,
        execution_status="pending",
    )
    defaults.update(kw)
    return CallResultImportRow(**defaults)


def test_refusal_comment_payload():
    row = _row(
        final_category="refusal",
        business_signals={"explicit_refusal": True, "summary": "Не интересно", "refusal_reason": "Нет потребности"},
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="crm.timeline.comment.add",
        action_type="timeline_comment",
        operation_type="bitrix_add_comment",
        payload={},
        human_summary="",
    )
    payload = BitrixPayloadBuilder().build(pa, row, bitrix_deal_id=1001, assigned_by_id=42, service_user_id=1)
    assert payload["fields"]["ENTITY_ID"] == 1001
    assert "Отказ" in payload["fields"]["COMMENT"]
    assert "Рекомендуется к ручному обзвону" not in payload["fields"]["COMMENT"]
    assert "primary_outcome" not in payload["fields"]["COMMENT"]


def test_positive_comment_payload():
    row = _row(
        primary_outcome="positive",
        business_signals={"positive": True, "summary": "Нужно КП"},
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="crm.timeline.comment.add",
        action_type="timeline_comment",
        operation_type="bitrix_add_comment",
        payload={},
        human_summary="",
    )
    payload = BitrixPayloadBuilder().build(pa, row, bitrix_deal_id=1001, assigned_by_id=42, service_user_id=1)
    assert "Положительный результат" in payload["fields"]["COMMENT"]
    assert "Нужно КП" in payload["fields"]["COMMENT"]
    assert "Рекомендуется к ручному обзвону" not in payload["fields"]["COMMENT"]


def test_callback_later_comment_payload():
    cb = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    row = _row(
        primary_outcome="callback_later",
        callback_at=cb,
        business_signals={"callback_later_requested": True, "summary": "Перезвонить завтра"},
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="crm.timeline.comment.add",
        action_type="timeline_comment",
        operation_type="bitrix_add_comment",
        payload={},
        human_summary="",
    )
    payload = BitrixPayloadBuilder().build(pa, row, bitrix_deal_id=1001, assigned_by_id=42, service_user_id=1)
    comment = payload["fields"]["COMMENT"]
    assert "Запрос перезвона" in comment
    assert "Запрошенный перезвон:" in comment
    assert "Рекомендуется к ручному обзвону" in comment


def test_hangup_comment_payload():
    row = _row(
        primary_outcome="hangup",
        business_signals={"hangup_without_result": True, "summary": "Сбросили"},
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="crm.timeline.comment.add",
        action_type="timeline_comment",
        operation_type="bitrix_add_comment",
        payload={},
        human_summary="",
    )
    context_actions = [
        PlannedAction(
            method="contact_search.add",
            action_type="contact_search_queue_add",
            operation_type="contact_search_queue_add",
            payload={},
            human_summary="",
        ),
    ]
    payload = BitrixPayloadBuilder().build(
        pa,
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        service_user_id=1,
        context_actions=context_actions,
    )
    assert "Сброс / нет результата" in payload["fields"]["COMMENT"]
    assert "Рекомендуется к ручному обзвону" in payload["fields"]["COMMENT"]


def test_hangup_during_robocall_comment_payload():
    row = _row(
        primary_outcome="hangup_during_robocall",
        business_signals={
            "hangup_during_robocall": True,
            "summary": "дозвон был, человек бросил трубку",
        },
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="crm.timeline.comment.add",
        action_type="timeline_comment",
        operation_type="bitrix_add_comment",
        payload={},
        human_summary="",
    )
    context_actions = [
        PlannedAction(
            method="crm.timeline.comment.add",
            action_type="timeline_comment",
            operation_type="bitrix_add_comment",
            payload={},
            human_summary="",
        ),
    ]
    payload = BitrixPayloadBuilder().build(
        pa,
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        service_user_id=1,
        context_actions=context_actions,
    )
    comment = payload["fields"]["COMMENT"]
    assert "Бросил без разговора" in comment
    assert "дозвон был, человек бросил трубку" in comment
    assert "Рекомендуется к ручному обзвону" in comment


def test_todo_payload_without_responsible_id():
    row = _row(
        primary_outcome="positive",
        business_signals={"positive": True, "summary": "Нужно КП"},
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="crm.activity.todo.add",
        action_type="crm_todo",
        operation_type="bitrix_add_todo",
        payload={},
        human_summary="",
    )
    payload = BitrixPayloadBuilder().build(pa, row, bitrix_deal_id=1001, assigned_by_id=999, service_user_id=1)
    assert "responsibleId" not in payload
    v = BitrixPayloadValidator().validate("crm.activity.todo.add", payload)
    assert v.status in ("valid", "warning")


def test_task_payload_valid():
    row = _row(
        primary_outcome="positive",
        business_signals={"positive": True, "summary": "Нужно КП"},
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    pa = PlannedAction(
        method="tasks.task.add",
        action_type="task",
        operation_type="bitrix_add_task",
        payload={},
        human_summary="",
    )
    payload = BitrixPayloadBuilder().build(pa, row, bitrix_deal_id=1001, assigned_by_id=42, service_user_id=1)
    fields = payload["fields"]
    assert fields["TITLE"]
    assert fields["UF_CRM_TASK"] == ["D_1001"]
    assert "ID сделки: 1001" in fields["DESCRIPTION"]
    assert "Нужно КП" in fields["DESCRIPTION"]
    v = BitrixPayloadValidator().validate("tasks.task.add", payload)
    assert v.status in ("valid", "warning")


def test_task_payload_missing_title_invalid():
    v = BitrixPayloadValidator().validate("tasks.task.add", {"fields": {}})
    assert v.status == "invalid"


def test_contact_fields_include_detailed_comments():
    from app.config import get_settings
    from app.services.call_results.crm_action_service import CrmActionService

    row = _row(
        call_id="call-xyz",
        primary_outcome="alternate_contact",
        business_signals={
            "alternate_contact_requested": True,
            "summary": "Просил перезвонить директору",
        },
        called_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
    )
    ac = {"name": "Иван Петров", "phone": "+79001234567", "position": "Директор"}
    svc = CrmActionService.__new__(CrmActionService)
    svc.settings = get_settings()
    comments = svc._contact_fields(ac, row)["COMMENTS"]
    assert "автоматического обзвона" in comments
    assert "+79001234567" in comments
    assert "Директор" in comments
    assert "Просил перезвонить директору" in comments
    assert "source=anechka_call" in comments


def test_relative_deadline_tomorrow():
    called = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)
    r = CallbackDateResolver().resolve("завтра в 15:00", None, called)
    assert r.callback_at is not None
    assert r.callback_at.day == 30


def test_ambiguous_deadline_autumn():
    called = datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)
    r = CallbackDateResolver().resolve("осенью", None, called)
    assert r.callback_at is None
    assert r.is_ambiguous
