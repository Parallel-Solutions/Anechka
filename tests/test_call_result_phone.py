"""Unit tests for phone normalization."""

from app.services.call_results.phone_normalizer import parse_phone_with_extension


def test_normalize_russian_8():
    p = parse_phone_with_extension("8 (912) 345-67-89")
    assert p.normalized == "79123456789"
    assert p.is_valid


def test_extension_split():
    p = parse_phone_with_extension("8 912 345 67 89 доб. 123")
    assert p.normalized == "79123456789"
    assert p.extension == "123"


def test_multiple_phones_invalid():
    p = parse_phone_with_extension("89991234567, 89997654321")
    assert not p.is_valid
    assert p.multi_status == "multiple"


def test_invalid_short():
    p = parse_phone_with_extension("12345")
    assert not p.is_valid
