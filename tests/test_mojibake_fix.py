"""Tests for mojibake repair helpers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.fix_mojibake import fix_json_value, fix_mojibake, fix_text_value, looks_like_mojibake


def test_looks_like_mojibake_detects_box_drawing():
    assert looks_like_mojibake("╨б╨┤╨╡╨╗╨║╨╕")
    assert not looks_like_mojibake("Сделки")
    assert not looks_like_mojibake("")
    assert not looks_like_mojibake("plain ascii")


def test_fix_mojibake_examples_from_seed():
    assert fix_mojibake("╨б╨┤╨╡╨╗╨║╨╕") == "Сделки"
    assert fix_mojibake("╨Ч╨░╨▓╨╡╤А╤И╨╡╨╜╨╛") == "Завершено"
    assert fix_mojibake("╨б╨┤╨╡╨╗╨║╨╕ ╨┐╨╛ ╤Б╤В╨░╨┤╨╕╨╕") == "Сделки по стадии"


def test_fix_mojibake_idempotent_for_valid_utf8():
    original = "Новый диалог"
    assert fix_mojibake(original) == original


def test_fix_json_value_recursively():
    payload = {
        "title": "╨б╨┤╨╡╨╗╨║╨╕",
        "items": ["╨Ч╨░╨▓╨╡╤А╤И╨╡╨╜╨╛", "ok"],
        "meta": {"note": "╨Я╨╛╨║╨░╨╢╨╕"},
    }
    fixed = fix_json_value(payload)
    assert fixed["title"] == "Сделки"
    assert fixed["items"][0] == "Завершено"
    assert fixed["items"][1] == "ok"
    assert fixed["meta"]["note"] == "Покажи"


def test_fix_mojibake_mixed_valid_and_corrupted_segments():
    mixed = (
        "Фильтры: Сделки.DATE_CREATE (deal) >= «2026-05-27»; "
        "Сделки.╨Ч╨░╨║╤А╤Л╤В╨╕╨╡ (deal) равно «N»; "
        "Сделки.╨Т╨╛╤А╨╛╨╜╨║╨░ (deal) равно «15»."
    )
    fixed = fix_mojibake(mixed)
    assert "Сделки.DATE_CREATE" in fixed
    assert "Закрытие" in fixed
    assert "Воронка" in fixed
    assert "╨" not in fixed


def test_fix_text_value_for_json_string_column():
    raw = json.dumps(["╨Ч╨░╨┤╨░╤З╨░ ╨╖╨░╨┐╤Г╤Й╨╡╨╜╨░"], ensure_ascii=False)
    fixed = fix_text_value(raw)
    assert json.loads(fixed) == ["Задача запущена"]
