"""Unit and integration tests for Tomoru call result adapter."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app.services.call_results.adapters.batch_filename import parse_batch_filename
from app.services.call_results.adapters.tomoru import TomoruCallResultAdapter
from app.services.call_results.call_attempt_aggregator import exact_duplicate_key
from app.services.call_results.contact_number_extractor import extract_contact_number
from app.services.call_results.deterministic_pre_classifier import DeterministicPreClassifier
from app.services.call_results.format_detector import FormatDetector
from app.services.call_results.llm_gate import LlmGate
from app.services.call_results.phone_normalizer import parse_phone_with_extension, parse_phones

FIXTURES = Path(__file__).parent / "fixtures" / "call_results" / "tomoru"
HEADERS = [
    "phone_number", "status_display", "call_result_display", "attempts", "last_attempt_at",
    "data:Вход", "data:Вход в диалог 2", "data:Выход на лпр", "data:номер ЛПР",
    "data:Перевод", "data:Вопрос 1", "data:Вопрос 2", "data:ТЗ",
    "data:Когда закупка", "data:Когда перезвонить лпр",
]


def test_tomoru_format_detect():
    assert TomoruCallResultAdapter.is_tomoru_format(HEADERS)
    assert not TomoruCallResultAdapter.is_tomoru_format(["phone", "comment"])


def test_batch_filename_parse():
    meta = parse_batch_filename("batch_2b01bb6f_20260629T213818(1).csv")
    assert meta.batch_id == "2b01bb6f"
    assert meta.exported_at == datetime(2026, 6, 29, 21, 38, 18)


def test_batch_filename_invalid():
    meta = parse_batch_filename("random.csv")
    assert meta.batch_id is None
    assert meta.warning


def test_parse_json_event():
    adapter = TomoruCallResultAdapter()
    row = {
        "phone_number": "73436053001",
        "status_display": "Completed",
        "call_result_display": "Interrupted",
        "attempts": "1",
        "last_attempt_at": "2026-06-15T10:06:50Z",
        "data:Выход на лпр": '{"match": "отдел архитектуры", "transcription": "Отдел архитектуры"}',
    }
    result = adapter.normalize_row(row, HEADERS, batch_id="abc")
    assert result.normalized["has_meaningful_content"]
    assert len(result.normalized["scenario_events"]) == 1
    assert "отдел архитектуры" in result.normalized["content_text"]


def test_dedup_content_text():
    adapter = TomoruCallResultAdapter()
    row = {
        "phone_number": "73436053001",
        "status_display": "Completed",
        "call_result_display": "Interrupted",
        "attempts": "1",
        "last_attempt_at": "2026-06-15T10:06:50Z",
        "data:Вход": '{"match": "same", "transcription": "same text"}',
        "data:Выход на лпр": '{"match": "same", "transcription": "same text"}',
    }
    result = adapter.normalize_row(row, HEADERS)
    assert result.normalized["content_text"].count("same text") == 1


def test_broken_json_warning():
    adapter = TomoruCallResultAdapter()
    row = {
        "phone_number": "73436053001",
        "status_display": "Completed",
        "call_result_display": "Interrupted",
        "attempts": "1",
        "last_attempt_at": "2026-06-15T10:06:50Z",
        "data:Вход": "{broken",
    }
    result = adapter.normalize_row(row, HEADERS)
    assert result.warnings
    assert result.normalized["scenario_events"]


def test_no_answer_no_llm_tomoru():
    pre = DeterministicPreClassifier().classify({
        "call_result": "No Answer",
        "status": "Not Started",
        "scenario_events": [],
        "has_meaningful_content": False,
    })
    assert not pre.unsupported_outcome
    assert pre.det_signals and pre.det_signals.no_answer
    assert not LlmGate.needs_llm({}, pre, llm_enabled=True)


def test_interrupted_with_data_llm():
    row = {
        "call_result": "Interrupted",
        "scenario_events": [{"field": "Выход на лпр", "match": "архитектура"}],
        "has_meaningful_content": True,
    }
    pre = DeterministicPreClassifier().classify(row)
    assert pre.llm_required


def test_interrupted_hangup_with_vhod_deterministic():
    """Interrupted + короткий вход без бизнес-сигналов → hangup_during_robocall."""
    import csv
    import io

    text = (FIXTURES / "interrupted_hangup_with_vhod.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    raw_row = next(reader)
    adapter = TomoruCallResultAdapter()
    tr = adapter.normalize_row(raw_row, HEADERS)
    assert tr.normalized["has_meaningful_content"]

    pre = DeterministicPreClassifier().classify(tr.normalized)
    assert not pre.llm_required
    assert pre.category == "robot_callback"
    assert pre.det_signals
    assert pre.det_signals.hangup_during_robocall
    assert not pre.det_signals.hangup_without_result
    assert pre.det_signals.summary == "дозвон был, человек бросил трубку"

    from app.models import CallResultImportRow
    from app.services.call_results.action_planner import BitrixActionPlanner

    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=3, raw_data=raw_row, normalized_data=tr.normalized,
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1001,
    )
    actions = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=pre.det_signals,
        requires_manual=False,
    )
    assert [a.method for a in actions] == []


def test_interrupted_department_greeting_is_hangup_without_result():
    """Interrupted + шаблонный match / приветствие отдела без диалога → hangup_without_result."""
    import csv
    import io

    text = (FIXTURES / "interrupted_department_greeting.csv").read_text(encoding="utf-8")
    reader = csv.DictReader(io.StringIO(text))
    raw_row = next(reader)
    adapter = TomoruCallResultAdapter()
    tr = adapter.normalize_row(raw_row, HEADERS)
    assert tr.normalized["has_meaningful_content"]

    pre = DeterministicPreClassifier().classify(tr.normalized)
    assert not pre.llm_required
    assert pre.category == "robot_callback"
    assert pre.det_signals
    assert pre.det_signals.hangup_without_result
    assert not pre.det_signals.replacement_contact_required
    assert not pre.det_signals.hangup_during_robocall

    from app.models import CallResultImportRow
    from app.services.call_results.action_planner import BitrixActionPlanner
    from app.services.call_results.row_disposition import get_row_disposition

    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=4, raw_data=raw_row, normalized_data=tr.normalized,
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1001,
    )
    actions = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=pre.det_signals,
        requires_manual=False,
    )
    assert [a.method for a in actions] == ["crm.timeline.comment.add"]
    assert get_row_disposition(row, actions) == "manual_review"


def test_do_not_call_refusal():
    pre = DeterministicPreClassifier().classify({"call_result": "Do Not Call"})
    assert pre.category == "refusal"


def test_extension_not_full_phone():
    r = extract_contact_number("20207", source_text="два ноль два ноль семь")
    assert r.extension == "20207"
    assert r.full_phone is None
    assert r.requires_review


def test_multiple_phones_not_merged():
    p = parse_phones("89991234567, 89997654321")
    assert p.status == "multiple"
    assert len(p.phones) == 2
    single = parse_phone_with_extension("89991234567, 89997654321")
    assert not single.is_valid


def test_exact_duplicate_key_diff_attempt():
    k1 = exact_duplicate_key(
        source_format="tomoru_csv", batch_id="b1", normalized_phone="79991234567",
        last_attempt_at=datetime(2026, 6, 10, 10, 0), technical_status="Completed",
        call_result_display="Interrupted", scenario_events=[],
    )
    k2 = exact_duplicate_key(
        source_format="tomoru_csv", batch_id="b1", normalized_phone="79991234567",
        last_attempt_at=datetime(2026, 6, 12, 10, 0), technical_status="Not Started",
        call_result_display="No Answer", scenario_events=[],
    )
    assert k1 != k2


def test_format_detector_tomoru():
    fmt = FormatDetector.detect(HEADERS, "batch_abc_20260629T120000.csv")
    assert fmt.is_tomoru
    assert fmt.auto_mapping.get("phone") == "phone_number"


def test_no_answer_real_batch_row():
    """Regression: Tomoru row like batch_ab6e9484 (78514236641, No Answer, attempts=0)."""
    adapter = TomoruCallResultAdapter()
    raw_row = {
        "phone_number": "78514236641",
        "status_display": "Not Started",
        "call_result_display": "No Answer",
        "attempts": "0",
        "last_attempt_at": "",
    }
    tr = adapter.normalize_row(raw_row, HEADERS)
    assert not tr.normalized["has_meaningful_content"]

    pre = DeterministicPreClassifier().classify(tr.normalized)
    assert pre.det_signals and pre.det_signals.no_answer
    assert not pre.unsupported_outcome

    from app.models import CallResultImportRow
    from app.services.call_results.action_planner import BitrixActionPlanner

    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=2, raw_data=raw_row, normalized_data=tr.normalized,
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1001,
    )
    actions = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=pre.det_signals,
        requires_manual=False,
    )
    assert [a.method for a in actions] == ["retry_queue.add"]
    assert actions[0].payload.get("reason") == "no_answer"


def test_interrupted_empty_deterministic_hangup():
    """Regression: Interrupted with empty data:* fields — hangup without LLM."""
    adapter = TomoruCallResultAdapter()
    raw_row = {
        "phone_number": "78422272914",
        "status_display": "Completed",
        "call_result_display": "Interrupted",
        "attempts": "1",
        "last_attempt_at": "2026-06-11T11:27:48Z",
        "data:Вход": "",
        "data:Вопрос 1": "",
        "data:Вопрос 2": "",
    }
    tr = adapter.normalize_row(raw_row, HEADERS)
    assert not tr.normalized["has_meaningful_content"]

    pre = DeterministicPreClassifier().classify(tr.normalized)
    assert not pre.llm_required
    assert pre.category == "robot_callback"
    assert pre.det_signals
    assert pre.det_signals.hangup_without_result
    assert not pre.det_signals.replacement_contact_required
    assert not pre.det_signals.no_answer

    from app.models import CallResultImportRow
    from app.services.call_results.action_planner import BitrixActionPlanner

    row = CallResultImportRow(
        id=1, import_id=1, source_row_number=53, raw_data=raw_row, normalized_data=tr.normalized,
        match_status="matched", llm_status="not_required", llm_required=False,
        manually_overridden=False, llm_input_truncated=False, is_duplicate=False,
        needs_manual_review=False, execution_status="pending", matched_deal_id=1001,
    )
    actions = BitrixActionPlanner().plan(
        row,
        bitrix_deal_id=1001,
        assigned_by_id=42,
        signals=pre.det_signals,
        requires_manual=False,
    )
    ops = [a.operation_type for a in actions]
    assert ops == ["bitrix_add_comment"]


def test_interrupted_empty_fixture():
    text = (FIXTURES / "interrupted_empty.csv").read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    headers = lines[0].split(",")
    values = lines[1].split(",")
    raw_row = dict(zip(headers, values))
    adapter = TomoruCallResultAdapter()
    tr = adapter.normalize_row(raw_row, headers)
    assert tr.normalized["phone"] == "78422272914"
    assert not tr.normalized["has_meaningful_content"]
    pre = DeterministicPreClassifier().classify(tr.normalized)
    assert pre.det_signals and pre.det_signals.hangup_without_result


@pytest.mark.parametrize("name", [
    "no_answer.csv", "interrupted_with_data.csv", "do_not_call.csv", "interrupted_empty.csv",
    "interrupted_hangup_with_vhod.csv",
    "refusal_vhod.csv", "callback_only.csv",
])
def test_fixture_files_parse(name):
    text = (FIXTURES / name).read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    headers = lines[0].split(",")
    assert TomoruCallResultAdapter.is_tomoru_format(headers)


def test_refusal_vhod_fixture_signals():
    from app.services.call_results.scenario_signal_extractor import extract_signals_from_scenario_events

    text = (FIXTURES / "refusal_vhod.csv").read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    headers = lines[0].split(",")
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    raw_row = next(reader)
    tr = TomoruCallResultAdapter().normalize_row(raw_row, headers)
    sig = extract_signals_from_scenario_events(tr.normalized)
    assert sig and sig.explicit_refusal


def test_callback_only_fixture_signals():
    from app.services.call_results.scenario_signal_extractor import extract_signals_from_scenario_events

    text = (FIXTURES / "callback_only.csv").read_text(encoding="utf-8")
    lines = text.strip().splitlines()
    headers = lines[0].split(",")
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    raw_row = next(reader)
    tr = TomoruCallResultAdapter().normalize_row(raw_row, headers)
    sig = extract_signals_from_scenario_events(tr.normalized)
    assert sig and sig.callback_later_requested


PORTAL = "example.bitrix24.ru"
HANGUP_PHONE = "78422272914"
REPLACEMENT_PHONE = "89169999999"


def _seed_hangup_crm(db, *, with_replacement: bool):
    from app.models import CrmContact, CrmContactLink, CrmContactPhone, CrmEntity, ENTITY_DEAL

    deal_id = 1001
    source_cid = 5001
    db.add(
        CrmEntity(
            portal_id=PORTAL,
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            title="Deal hangup",
            assigned_by_id=42,
            raw_payload={"closed": "N"},
            payload_hash="hash-hangup",
        )
    )
    db.add(
        CrmContact(
            portal_id=PORTAL,
            contact_id=source_cid,
            full_name="Исходный контакт",
        )
    )
    db.add(
        CrmContactPhone(
            portal_id=PORTAL,
            contact_id=source_cid,
            value=HANGUP_PHONE,
            value_type="MOBILE",
            is_primary=True,
        )
    )
    db.add(
        CrmContactLink(
            portal_id=PORTAL,
            contact_id=source_cid,
            parent_entity_type_id=ENTITY_DEAL,
            parent_entity_id=deal_id,
            is_primary=True,
        )
    )
    if with_replacement:
        replacement_cid = 6099
        db.add(
            CrmContact(
                portal_id=PORTAL,
                contact_id=replacement_cid,
                full_name="Генеральный директор",
                post="Генеральный директор",
            )
        )
        db.add(
            CrmContactPhone(
                portal_id=PORTAL,
                contact_id=replacement_cid,
                value=REPLACEMENT_PHONE,
                value_type="MOBILE",
                is_primary=True,
            )
        )
        db.add(
            CrmContactLink(
                portal_id=PORTAL,
                contact_id=replacement_cid,
                parent_entity_type_id=ENTITY_DEAL,
                parent_entity_id=deal_id,
                is_primary=False,
            )
        )
    db.commit()


def _process_interrupted_empty(db_session):
    from app.config import get_settings
    from app.services.call_results.fake_classifier import FakeCallResultClassifier
    from app.services.call_results.orchestrator import CallResultOrchestrator

    content = (FIXTURES / "interrupted_empty.csv").read_bytes()
    settings = get_settings()
    orch = CallResultOrchestrator(
        db_session,
        settings,
        PORTAL,
        FakeCallResultClassifier([]),
    )
    imp, _ = orch.save_uploaded_file(content, "interrupted_empty.csv")
    db_session.commit()
    orch.process_import(imp.id)
    db_session.commit()
    row = orch.repo.list_rows(imp.id)[0]
    actions = [a for a in orch.repo.list_actions(imp.id) if a.import_row_id == row.id]
    return imp, row, actions


def test_hangup_without_answer_gets_comment(db_session):
    from app.services.call_results.row_disposition import get_row_disposition
    from app.services.call_results.row_filter import get_row_filter

    _seed_hangup_crm(db_session, with_replacement=True)
    _, row, actions = _process_interrupted_empty(db_session)

    assert (row.business_signals or {}).get("hangup_without_result")
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"
    assert get_row_disposition(row, actions) == "manual_review"
    assert get_row_filter(row, actions) == "new_comments"

    methods = [a.method for a in actions if a.is_enabled]
    assert methods == ["crm.timeline.comment.add"]
    assert "contact_search.add" not in methods
    assert "retry_queue.add" not in methods


def test_hangup_without_answer_comment_without_replacement_contact(db_session):
    from app.services.call_results.row_disposition import get_row_disposition
    from app.services.call_results.row_filter import get_row_filter

    _seed_hangup_crm(db_session, with_replacement=False)
    _, row, actions = _process_interrupted_empty(db_session)

    assert (row.business_signals or {}).get("hangup_without_result")
    assert row.needs_manual_review is False
    assert row.execution_status == "prepared"
    assert get_row_disposition(row, actions) == "manual_review"
    assert get_row_filter(row, actions) == "new_comments"
    assert [a.method for a in actions if a.is_enabled] == ["crm.timeline.comment.add"]

