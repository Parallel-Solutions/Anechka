"""Action planner v2 unit tests."""

from app.models import CallResultImportRow
from app.services.call_results.action_planner import BitrixActionPlanner
from app.services.call_results.llm_schema import CallResultSignals


def _row(**kw):
    defaults = dict(
        id=1,
        import_id=1,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        raw_phone="+79161234567",
        normalized_phone="9161234567",
        match_status="matched",
        llm_status="not_required",
        llm_required=False,
        manually_overridden=False,
        llm_input_truncated=False,
        is_duplicate=False,
        needs_manual_review=False,
        execution_status="pending",
        matched_deal_id=1001,
        matched_contact_id=55,
    )
    defaults.update(kw)
    return CallResultImportRow(**defaults)


def _plan(signals: CallResultSignals, *, requires_manual: bool = False, **row_kw):
    return BitrixActionPlanner().plan(
        _row(**row_kw),
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=signals,
        requires_manual=requires_manual,
    )


def test_positive_only_todo():
    actions = _plan(CallResultSignals(positive=True, summary="Нужно КП", confidence=0.9))
    methods = [a.method for a in actions]
    assert methods == ["crm.activity.todo.add"]
    assert "crm.timeline.comment.add" not in methods
    assert "tasks.task.add" not in methods


def test_refusal_only_comment():
    actions = _plan(CallResultSignals(explicit_refusal=True, summary="Не нужно", confidence=0.9))
    methods = [a.method for a in actions]
    assert methods == ["crm.timeline.comment.add"]
    assert "crm.activity.todo.add" not in methods
    assert "retry_queue.add" not in methods


def test_callback_later_only_retry():
    actions = _plan(
        CallResultSignals(callback_later_requested=True, callback_text="завтра", confidence=0.9),
        callback_at=None,
    )
    methods = [a.method for a in actions]
    assert methods == ["retry_queue.add"]
    assert "crm.activity.todo.add" not in methods
    assert "crm.timeline.comment.add" not in methods


def test_no_answer_only_retry():
    actions = _plan(CallResultSignals(no_answer=True, summary="Не дозвонились: No Answer", confidence=1.0))
    methods = [a.method for a in actions]
    assert methods == ["retry_queue.add"]
    retry = actions[0]
    assert retry.payload.get("reason") == "no_answer"
    assert retry.human_summary == "Не дозвонились — очередь повторов"


def test_alternate_contact_full_flow():
    actions = _plan(
        CallResultSignals(
            alternate_contact_requested=True,
            alternate_contact={"name": "Иван", "phone": "+79001234567", "extension": None, "email": None, "position": None},
            confidence=0.9,
        )
    )
    ops = [a.operation_type for a in actions]
    assert ops == [
        "bitrix_find_contact",
        "bitrix_create_contact",
        "bitrix_link_contact_to_deal",
        "retry_queue_add",
    ]


def test_hangup_contact_search_and_retry():
    actions = _plan(CallResultSignals(hangup_without_result=True, confidence=0.8))
    assert len(actions) == 2
    ops = [a.operation_type for a in actions]
    assert ops == ["contact_search_queue_add", "retry_queue_add"]
    assert actions[0].method == "contact_search.add"
    assert actions[1].payload.get("reason") == "hangup_replacement_contact"
    assert actions[1].payload.get("search_required") is True
    assert actions[0].method != "crm.timeline.comment.add"


def test_manual_review_no_bitrix():
    actions = _plan(CallResultSignals(needs_manual_review=True, manual_review_reason="test"), requires_manual=True)
    assert len(actions) == 1
    assert actions[0].operation_type == "manual_review_required"
    assert not actions[0].is_enabled
