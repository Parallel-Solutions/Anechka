"""Fixtures for Bitrix import tests."""

from __future__ import annotations

SAMPLE_LEAD = {
    "id": 101,
    "title": "Test Lead",
    "updatedTime": "2026-06-01T10:00:00+03:00",
    "assignedById": 1,
    "UF_CRM_CUSTOM": "value1",
}

SAMPLE_DEAL = {
    "id": 201,
    "title": "Test Deal",
    "updatedTime": "2026-06-01T11:00:00+03:00",
    "stageId": "NEW",
    "categoryId": 0,
    "opportunity": 1000,
}

SAMPLE_FIELDS = {
    "title": {"type": "string", "title": "Название", "isRequired": True},
    "UF_CRM_CUSTOM": {
        "type": "enumeration",
        "title": "Custom Field",
        "items": [{"ID": "1", "VALUE": "Option A"}, {"ID": "2", "VALUE": "Option B"}],
    },
}

SAMPLE_FIELDS_V2 = {
    **SAMPLE_FIELDS,
    "UF_CRM_NEW": {"type": "string", "title": "New Field"},
}

SAMPLE_DEAL_UPDATED = {
    **SAMPLE_DEAL,
    "title": "Updated Deal",
    "updatedTime": "2026-06-02T12:00:00+03:00",
}

SAMPLE_DEAL_SAME_TIME_1 = {
    "id": 301,
    "title": "Deal A",
    "updatedTime": "2026-06-01T15:00:00+03:00",
}

SAMPLE_DEAL_SAME_TIME_2 = {
    "id": 302,
    "title": "Deal B",
    "updatedTime": "2026-06-01T15:00:00+03:00",
}

AI_FIELD_RESPONSE = {
    "fields": [
        {
            "field_code": "UF_CRM_CUSTOM",
            "display_name": "Пользовательское поле",
            "short_description": "Кастомное поле",
            "detailed_description": "Описание",
            "business_purpose": "Классификация",
            "normalized_data_type": "string",
            "data_category": "classification",
            "is_dictionary": True,
            "dictionary_kind": "enumeration",
            "nullable_description": "Может быть пустым",
            "confidence": 0.8,
            "needs_review": False,
            "warnings": [],
        }
    ]
}

INVALID_AI_RESPONSE = {"fields": [{"field_code": "x"}]}
