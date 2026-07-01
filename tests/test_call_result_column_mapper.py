"""Unit tests for call result column mapper."""

from app.services.call_results.column_mapper import CallResultColumnMapper


def test_map_phone_column_ru():
    mapper = CallResultColumnMapper()
    result = mapper.map_headers(["Телефон", "Комментарий", "Категория"])
    assert result.mapping.get("phone") == "Телефон"
    assert result.mapping.get("comment") == "Комментарий"
    assert not result.needs_manual


def test_missing_phone_requires_manual():
    mapper = CallResultColumnMapper()
    result = mapper.map_headers(["Имя", "Город"])
    assert result.needs_manual
    assert "phone" not in result.mapping


def test_user_mapping_override():
    mapper = CallResultColumnMapper()
    result = mapper.map_headers(["X"], {"phone": "X"})
    assert result.mapping["phone"] == "X"
