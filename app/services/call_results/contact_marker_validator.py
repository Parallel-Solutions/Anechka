"""Validate Bitrix contact source marker configuration."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.services.bitrix_client import BitrixClient


@dataclass
class MarkerValidation:
    configured: bool
    validated: bool
    field_code: str
    field_value: str
    error: str | None = None
    warning: str | None = None


class ContactMarkerValidator:
    def __init__(self, settings: Settings, client: BitrixClient | None = None):
        self.settings = settings
        self.client = client

    def validate(self) -> MarkerValidation:
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
