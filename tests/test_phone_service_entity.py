"""Tests for entity payload phone extraction."""

from app.services.phone_service import extract_phones_from_entity_payload, extract_phones_from_multifield


def test_extract_phones_from_multifield_string():
    result = extract_phones_from_multifield("83533534219")
    assert len(result) == 1
    assert result[0][0] == "83533534219"


def test_extract_phones_from_entity_payload_string_phone():
    raw = {"phone": "83533534219", "phoneWork": "83533534219"}
    result = extract_phones_from_entity_payload(raw)
    assert len(result) == 1
    assert result[0][0] == "83533534219"


def test_extract_phones_from_entity_payload_fm():
    raw = {
        "fm": [
            {"id": 1, "value": "83533534219", "typeId": "PHONE", "valueType": "WORK"},
        ],
    }
    result = extract_phones_from_entity_payload(raw)
    assert len(result) == 1
    assert result[0][0] == "83533534219"
