"""Tests for contact phone sync deduplication."""

from app.repositories.contact_repository import ContactRepository

PORTAL = "example.bitrix24.ru"


def test_sync_phones_dedup_by_normalized_digits(db_session):
    repo = ContactRepository(db_session, PORTAL)
    repo.sync_phones(
        99,
        [{"value": "89991234567", "value_type": "MOBILE"}],
        primary_value="89991234567",
    )
    repo.sync_phones(
        99,
        [{"value": "+7 999 123-45-67", "value_type": "MOBILE"}],
        primary_value="+7 999 123-45-67",
    )
    db_session.commit()

    phones = repo.get_phones_for_contact(99)
    assert len(phones) == 1
    assert phones[0]["value"] == "+7 999 123-45-67"
