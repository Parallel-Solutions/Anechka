"""Чтение связей контакт<->сделка/лид в ContactRepository."""

from __future__ import annotations

from app.models import CrmEntity
from app.repositories.contact_repository import ContactRepository

PORTAL = "example.bitrix24.ru"


def _make_entity(db, entity_type_id, entity_id, title, stage="NEW"):
    e = CrmEntity(
        portal_id=PORTAL,
        entity_type_id=entity_type_id,
        entity_id=entity_id,
        entity_kind="deal" if entity_type_id == 2 else "lead",
        title=title,
        stage_id=stage,
        raw_payload={},
        payload_hash="h",
    )
    db.add(e)
    db.flush()
    return e


def test_contacts_for_parent_and_links_for_contact(db_session):
    repo = ContactRepository(db_session, PORTAL)
    # сделка #100 и контакт #5
    _make_entity(db_session, 2, 100, "Сделка А")
    repo.upsert_contact(5, {"full_name": "Иван Иванов", "primary_phone": "+70000000000"})
    repo.upsert_link(5, 2, 100, is_primary=True)
    db_session.flush()

    # прямая сторона: контакты сделки
    contacts = repo.get_contacts_for_parent(2, 100)
    assert len(contacts) == 1
    assert contacts[0]["contact"].full_name == "Иван Иванов"
    assert contacts[0]["link"].is_primary is True

    # обратная сторона: сделки контакта
    parents = repo.get_links_for_contact(5)
    assert len(parents) == 1
    assert parents[0]["link"].parent_entity_type_id == 2
    assert parents[0]["parent"].title == "Сделка А"


def test_empty_results(db_session):
    repo = ContactRepository(db_session, PORTAL)
    assert repo.get_contacts_for_parent(2, 999) == []
    assert repo.get_links_for_contact(999) == []
