"""Unit tests for scenario_signal_extractor fallback."""

from app.services.call_results.deterministic_pre_classifier import DeterministicPreClassifier, PreClassResult
from app.services.call_results.scenario_signal_extractor import extract_signals_from_scenario_events
from app.services.call_results.signal_merger import SignalMerger


def test_refusal_from_vhod_nyet():
    row = {
        "has_meaningful_content": True,
        "scenario_events": [{
            "field": "Вход",
            "source_column": "data:Вход",
            "match": "Нет",
            "transcription": "А мне оно нужно разве нет спасибо не нужно",
        }],
    }
    sig = extract_signals_from_scenario_events(row)
    assert sig is not None
    assert sig.explicit_refusal
    assert not sig.callback_later_requested


def test_callback_from_kogda_perezvonit():
    row = {
        "has_meaningful_content": True,
        "scenario_events": [{
            "field": "Когда перезвонить лпр",
            "source_column": "data:Когда перезвонить лпр",
            "match": "на той неделе в понедельник",
            "transcription": "Это только на той неделе говорю завтра уже выходные будут на той неделе в понедельник звоните",
        }],
    }
    sig = extract_signals_from_scenario_events(row)
    assert sig is not None
    assert sig.callback_later_requested
    assert "понедельник" in (sig.callback_text or "")


def test_alternate_contact_from_vyhod_na_lpr():
    row = {
        "has_meaningful_content": True,
        "scenario_events": [
            {
                "field": "Вход",
                "match": "Земельный комитет, Елена Алексеевна",
                "transcription": "Земельный комитет Алло...",
            },
            {
                "field": "Выход на лпр",
                "match": "Так личного номера нету могу стационарный дать",
                "transcription": "Так личного номера нету могу стационарный дать",
            },
        ],
    }
    sig = extract_signals_from_scenario_events(row)
    assert sig is not None
    assert sig.alternate_contact_requested


def test_no_signals_without_content():
    row = {"has_meaningful_content": False, "scenario_events": []}
    assert extract_signals_from_scenario_events(row) is None


def test_merger_fallback_when_llm_none():
    row = {
        "has_meaningful_content": True,
        "scenario_events": [{
            "field": "Вход",
            "match": "Нет",
            "transcription": "не нужно спасибо",
        }],
    }
    pre = PreClassResult(category=None, reason="Interrupted с содержательными данными — LLM", llm_required=True)
    merged = SignalMerger().merge(pre, None, llm_valid=False, normalized_data=row)
    assert merged.signals.explicit_refusal
    assert merged.classification_source == "deterministic"


def test_interrupted_empty_no_fallback():
    pre = DeterministicPreClassifier().classify({
        "call_result": "Interrupted",
        "scenario_events": [],
        "has_meaningful_content": False,
    })
    assert pre.det_signals and pre.det_signals.hangup_without_result
    merged = SignalMerger().merge(pre, None, llm_valid=False, normalized_data={
        "has_meaningful_content": False,
        "scenario_events": [],
    })
    assert merged.signals.hangup_without_result
    assert not merged.signals.explicit_refusal
