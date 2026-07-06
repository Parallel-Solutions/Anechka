"""Tests for ContactMarkerValidator caching."""

from unittest.mock import MagicMock

import pytest

from app.services.call_results.contact_marker_validator import (
    ContactMarkerValidator,
    clear_marker_validation_cache,
    get_contact_creation_allowed,
    get_marker_validation,
)


def _settings(**overrides):
    settings = MagicMock()
    settings.bitrix_webhook_url = "https://example.bitrix24.ru/rest/1/token"
    settings.bitrix_call_source_field_code = "UF_SOURCE"
    settings.bitrix_call_source_field_value = "call_bot"
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_marker_validation_cache()
    yield
    clear_marker_validation_cache()


def test_validate_uses_cache_on_second_call():
    client = MagicMock()
    client.call.return_value = {"result": {"UF_SOURCE": {"type": "string"}}}
    settings = _settings()
    validator = ContactMarkerValidator(settings, client=client)

    first = validator.validate()
    second = validator.validate()

    assert first.validated is True
    assert second.validated is True
    client.call.assert_called_once_with("crm.contact.fields")


def test_validate_cache_shared_via_helpers():
    client = MagicMock()
    client.call.return_value = {"result": {"UF_SOURCE": {"type": "string"}}}
    settings = _settings()

    marker = get_marker_validation(settings, client=client)
    allowed = get_contact_creation_allowed(settings, client=client)

    assert marker.validated is True
    assert allowed is True
    client.call.assert_called_once_with("crm.contact.fields")


def test_validate_caches_failed_bitrix_attempt():
    client = MagicMock()
    client.call.side_effect = RuntimeError("connection failed")
    settings = _settings()
    validator = ContactMarkerValidator(settings, client=client)

    first = validator.validate()
    second = validator.validate()

    assert first.validated is False
    assert "Не удалось проверить поле" in (first.warning or "")
    assert second.warning == first.warning
    client.call.assert_called_once_with("crm.contact.fields")


def test_contact_creation_allowed_without_bitrix_call_when_unconfigured():
    settings = _settings(
        bitrix_call_source_field_code="",
        bitrix_call_source_field_value="",
    )
    client = MagicMock()

    allowed = get_contact_creation_allowed(settings, client=client)

    assert allowed is False
    client.call.assert_not_called()
