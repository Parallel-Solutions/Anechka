"""Unit tests for tomoru_regions helpers."""

from __future__ import annotations

import pytest

from app.services.intelligent_export.tomoru_regions import (
    ORENBURG_REGION_SENTINEL,
    is_filter_placeholder,
    is_valid_tomoru_region_filter_value,
    resolve_region_filter_value,
    resolve_region_id_from_text,
    resolve_title_region_from_message,
    resolve_tomoru_region_from_message,
    try_parse_region_filter_value,
)


def test_is_filter_placeholder():
    assert is_filter_placeholder("<ID региона Санкт-Петербурга>") is True
    assert is_filter_placeholder("1107") is False


def test_resolve_region_id_from_placeholder():
    assert resolve_region_id_from_text("<ID региона Санкт-Петербурга>") == 1107


def test_try_parse_region_filter_value_numeric():
    assert try_parse_region_filter_value(1107) == 1107
    assert try_parse_region_filter_value("1107") == 1107


def test_resolve_orenburg_from_message():
    region = resolve_title_region_from_message("туморoу, Оренбург, стадия КП ушло - дошло ли КП?")
    assert region is not None
    assert region.key == "orenburg"
    assert region.legacy_uf_id == 171
    assert resolve_tomoru_region_from_message("туморoу, Оренбург, стадия КП ушло") is None


def test_orenburg_sentinel_not_numeric_region_id():
    assert try_parse_region_filter_value(ORENBURG_REGION_SENTINEL) is None
    with pytest.raises(ValueError):
        resolve_region_filter_value(ORENBURG_REGION_SENTINEL)


def test_is_valid_tomoru_region_filter_value():
    assert is_valid_tomoru_region_filter_value(ORENBURG_REGION_SENTINEL) is True
    assert is_valid_tomoru_region_filter_value(1107) is True
    assert is_valid_tomoru_region_filter_value("1107") is True
    assert is_valid_tomoru_region_filter_value("foo") is False
    assert is_valid_tomoru_region_filter_value("<ID региона X>") is False


def test_resolve_region_filter_value_unresolved_raises():
    with pytest.raises(ValueError):
        resolve_region_filter_value("<ID региона Неизвестный Город>")
