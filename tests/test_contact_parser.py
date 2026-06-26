"""Tests for contact_parser."""

from app.services.bitrix_import.contact_parser import (
    POST_CUSTOM_FIELD,
    build_full_name,
    choose_primary_phone,
    extract_contact_fields,
    normalize_phone_type,
    parse_phones,
)


def test_normalize_phone_type():
    assert normalize_phone_type("mobile") == "MOBILE"
    assert normalize_phone_type("WORK") == "WORK"
    assert normalize_phone_type("HOME") == "HOME"
    assert normalize_phone_type("FAX") == "OTHER"
    assert normalize_phone_type(None) == "OTHER"


def test_parse_phones_plain_string():
    phones = parse_phones("83532987871")
    assert len(phones) == 1
    assert phones[0]["value"] == "83532987871"
    assert phones[0]["value_type"] == "WORK"


def test_parse_phones_dedup_and_skip_empty():
    raw = [
        {"VALUE": "+79001112233", "VALUE_TYPE": "MOBILE"},
        {"VALUE": "+79001112233", "VALUE_TYPE": "WORK"},
        {"VALUE": "", "VALUE_TYPE": "WORK"},
        {"value": "+74951234567", "valueType": "WORK"},
    ]
    phones = parse_phones(raw)
    assert len(phones) == 2
    assert phones[0]["value"] == "+79001112233"
    assert phones[0]["value_type"] == "MOBILE"
    assert phones[1]["value"] == "+74951234567"


def test_choose_primary_phone():
    phones = [
        {"value": "+7495", "value_type": "WORK"},
        {"value": "+7900", "value_type": "MOBILE"},
        {"value": "+7499", "value_type": "HOME"},
    ]
    primary = choose_primary_phone(phones)
    assert primary["value"] == "+7900"
    assert primary["value_type"] == "MOBILE"

    phones_no_mobile = [
        {"value": "+7495", "value_type": "WORK"},
        {"value": "+7499", "value_type": "HOME"},
    ]
    primary = choose_primary_phone(phones_no_mobile)
    assert primary["value"] == "+7495"

    phones_other = [{"value": "+7499", "value_type": "OTHER"}]
    primary = choose_primary_phone(phones_other)
    assert primary["value"] == "+7499"


def test_build_full_name_skips_empty_parts():
    assert build_full_name({"LAST_NAME": "Иванов", "NAME": "Иван", "SECOND_NAME": ""}) == "Иванов Иван"
    assert build_full_name({"lastName": "Петров", "name": "Пётр", "secondName": "Сергеевич"}) == "Петров Пётр Сергеевич"


def test_extract_contact_fields():
    contact = {
        "LAST_NAME": "Иванов",
        "NAME": "Иван",
        "SECOND_NAME": "Иванович",
        "POST": "Директор",
        POST_CUSTOM_FIELD: "Главный инженер",
        "COMPANY_ID": "42",
        "PHONE": [
            {"VALUE": "+79001112233", "VALUE_TYPE": "MOBILE"},
            {"VALUE": "+74951234567", "VALUE_TYPE": "WORK"},
        ],
    }
    fields = extract_contact_fields(contact)
    assert fields["full_name"] == "Иванов Иван Иванович"
    assert fields["post"] == "Директор"
    assert fields["post_custom"] == "Главный инженер"
    assert fields["company_id"] == 42
    assert fields["primary_phone"] == "+79001112233"
    assert fields["primary_phone_type"] == "MOBILE"
    assert len(fields["phones"]) == 2
