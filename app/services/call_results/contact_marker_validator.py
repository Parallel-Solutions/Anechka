"""Validate Bitrix contact source marker configuration."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import ClassVar

from app.config import Settings
from app.services.bitrix_client import BitrixClient

_MARKER_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class MarkerValidation:
    configured: bool
    validated: bool
    field_code: str
    field_value: str
    error: str | None = None
    warning: str | None = None


@dataclass
class _CacheEntry:
    validation: MarkerValidation
    expires_at: float


class ContactMarkerValidator:
    _cache: ClassVar[dict[tuple[str, str, str], _CacheEntry]] = {}

    def __init__(self, settings: Settings, client: BitrixClient | None = None):
        self.settings = settings
        self.client = client

    def _cache_key(self) -> tuple[str, str, str]:
        code = getattr(self.settings, "bitrix_call_source_field_code", "") or ""
        value = getattr(self.settings, "bitrix_call_source_field_value", "") or ""
        webhook = self.settings.bitrix_webhook_url or ""
        return (webhook, code, value)

    def _get_cached(self) -> MarkerValidation | None:
        entry = self._cache.get(self._cache_key())
        if entry is None or time.monotonic() >= entry.expires_at:
            return None
        return entry.validation

    def _set_cached(self, validation: MarkerValidation) -> None:
        self._cache[self._cache_key()] = _CacheEntry(
            validation=validation,
            expires_at=time.monotonic() + _MARKER_CACHE_TTL_SECONDS,
        )

    def validate(self) -> MarkerValidation:
        cached = self._get_cached()
        if cached is not None:
            return cached
        result = self._validate_uncached()
        self._set_cached(result)
        return result

    def _validate_uncached(self) -> MarkerValidation:
        code = getattr(self.settings, "bitrix_call_source_field_code", "") or ""
        value = getattr(self.settings, "bitrix_call_source_field_value", "") or ""
        if not code or not value:
            return MarkerValidation(
                configured=False,
                validated=False,
                field_code=code,
                field_value=value,
                warning="BITRIX_CALL_SOURCE_FIELD_CODE/VALUE не заданы",
            )
        if not self.settings.bitrix_webhook_url:
            return MarkerValidation(
                configured=True,
                validated=False,
                field_code=code,
                field_value=value,
                warning="Webhook не настроен — автоматическая проверка поля невозможна",
            )
        client = self.client or BitrixClient(self.settings)
        try:
            data = client.call("crm.contact.fields")
            fields = data.get("result") or {}
            if code not in fields:
                return MarkerValidation(
                    configured=True,
                    validated=False,
                    field_code=code,
                    field_value=value,
                    error=f"Поле {code} не найдено в crm.contact.fields",
                )
            return MarkerValidation(
                configured=True,
                validated=True,
                field_code=code,
                field_value=value,
            )
        except Exception as exc:
            return MarkerValidation(
                configured=True,
                validated=False,
                field_code=code,
                field_value=value,
                warning=f"Не удалось проверить поле: {exc}",
            )

    def contact_creation_allowed(self) -> bool:
        v = self.validate()
        if not v.configured:
            return False
        if v.validated:
            return True
        # Allow if explicitly configured but webhook can't verify userfields
        return bool(v.field_code and v.field_value and v.error is None)


def get_marker_validation(
    settings: Settings,
    client: BitrixClient | None = None,
) -> MarkerValidation:
    return ContactMarkerValidator(settings, client=client).validate()


def get_contact_creation_allowed(
    settings: Settings,
    client: BitrixClient | None = None,
) -> bool:
    return ContactMarkerValidator(settings, client=client).contact_creation_allowed()


def clear_marker_validation_cache() -> None:
    ContactMarkerValidator._cache.clear()
