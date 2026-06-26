"""Tests for phone normalization."""

from app.services.phone_service import (
    PhoneSource,
    add_phone_entries,
    normalize_phone,
)


def test_normalize_russian_8_to_7():
    assert normalize_phone("8 (912) 345-67-89") == "79123456789"
    assert normalize_phone("+79123456789") == "79123456789"


def test_normalize_invalid():
    assert normalize_phone("") is None
    assert normalize_phone("123") is None


def test_dedup_within_contact():
    entries = []
    add_phone_entries(
        entries,
        [("89123456789", "MOBILE"), ("+7 912 345 67 89", "WORK")],
        PhoneSource.DEAL_CONTACT,
        contact_id=1,
        contact_name="Иванов",
    )
    assert len(entries) == 1


def test_same_phone_different_contacts():
    entries = []
    add_phone_entries(entries, [("89123456789", "")], PhoneSource.DEAL_CONTACT, contact_id=1, contact_name="A")
    add_phone_entries(entries, [("89123456789", "")], PhoneSource.DEAL_CONTACT, contact_id=2, contact_name="B")
    assert len(entries) == 2
