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


def test_tasks_forbidden():
    v = BitrixPayloadValidator().validate("tasks.task.add", {"fields": {"TITLE": "x", "RESPONSIBLE_ID": 1, "CREATED_BY": 1}})
    assert v.status == "invalid"


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
