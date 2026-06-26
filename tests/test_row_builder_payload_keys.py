"""Tests for raw_payload key resolution in row_builder."""

from __future__ import annotations

from types import SimpleNamespace

from app.models import ENTITY_CONTACT
from app.services.export_plan.catalog import FieldCatalog
from app.services.export_plan.models_v2 import ConcatValue, FieldRef, FieldValue
from app.services.export_plan.payload_keys import camel_key, payload_lookup
from app.services.intelligent_export.row_builder import get_field_raw, resolve_value

PORTAL = "test.bitrix24.ru"


def test_camel_key():
    assert camel_key("LAST_NAME") == "lastName"
    assert camel_key("SECOND_NAME") == "secondName"
    assert camel_key("PHONE") == "phone"


def test_payload_lookup_camel_case():
    raw = {
        "lastName": "Иванов",
        "name": "Иван",
        "secondName": "Иванович",
        "post": "Директор",
        "comments": "Описание",
    }
    assert payload_lookup(raw, "LAST_NAME") == "Иванов"
    assert payload_lookup(raw, "NAME") == "Иван"
    assert payload_lookup(raw, "SECOND_NAME") == "Иванович"
    assert payload_lookup(raw, "POST") == "Директор"
    assert payload_lookup(raw, "COMMENTS") == "Описание"


def _contact_row(raw_payload: dict) -> dict:
    entity = SimpleNamespace(
        entity_id=raw_payload.get("id", 1),
        title=raw_payload.get("title", "Contact"),
        raw_payload=raw_payload,
    )
    return {"contact": entity}


def test_get_field_raw_camel_case_fio(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    row = _contact_row(
        {
            "id": 10,
            "title": "Отдел архитектуры",
            "lastName": "Петров",
            "name": "Пётр",
            "secondName": "Сергеевич",
        }
    )
    assert get_field_raw(row, FieldRef(entity_type_id=ENTITY_CONTACT, field_code="LAST_NAME", source_alias="contact"), catalog) == "Петров"
    assert get_field_raw(row, FieldRef(entity_type_id=ENTITY_CONTACT, field_code="NAME", source_alias="contact"), catalog) == "Пётр"
    assert get_field_raw(row, FieldRef(entity_type_id=ENTITY_CONTACT, field_code="SECOND_NAME", source_alias="contact"), catalog) == "Сергеевич"


def test_get_field_raw_primary_phone_mobile_first(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    row = _contact_row(
        {
            "id": 11,
            "phone": [
                {"value": "work@example.com", "valueType": "WORK"},
                {"value": "89991112233", "valueType": "MOBILE"},
            ],
        }
    )
    phone = get_field_raw(
        row,
        FieldRef(entity_type_id=ENTITY_CONTACT, field_code="PHONE", source_alias="contact"),
        catalog,
    )
    assert phone == "89991112233"


def test_resolve_value_fio_concat(db_session):
    catalog = FieldCatalog.load(db_session, PORTAL)
    row = _contact_row(
        {
            "id": 12,
            "title": "Wrong Title",
            "lastName": "Сидоров",
            "name": "Сидор",
            "secondName": "Сидорович",
        }
    )
    value = ConcatValue(
        parts=[
            FieldValue(field=FieldRef(entity_type_id=ENTITY_CONTACT, field_code="LAST_NAME", source_alias="contact")),
            FieldValue(field=FieldRef(entity_type_id=ENTITY_CONTACT, field_code="NAME", source_alias="contact")),
            FieldValue(field=FieldRef(entity_type_id=ENTITY_CONTACT, field_code="SECOND_NAME", source_alias="contact")),
        ],
        separator=" ",
    )
    assert resolve_value(value, row, catalog) == "Сидоров Сидор Сидорович"
