"""Resolve deal timezone from CRM region field."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmEntity, CrmEntityFieldValue, CrmFieldDefinition
from app.services.export_plan.payload_keys import camel_key
from app.services.intelligent_export.contact_phone_heuristic import TOMORU_REGION_FIELD

# Region name (lowercase substring) -> IANA timezone
_REGION_TZ: dict[str, str] = {
    "москва": "Europe/Moscow",
    "московск": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "спб": "Europe/Moscow",
    "ленинград": "Europe/Moscow",
    "екатеринбург": "Asia/Yekaterinburg",
    "свердловск": "Asia/Yekaterinburg",
    "новосибирск": "Asia/Novosibirsk",
    "красноярск": "Asia/Krasnoyarsk",
    "иркутск": "Asia/Irkutsk",
    "владивосток": "Asia/Vladivostok",
    "калининград": "Europe/Kaliningrad",
    "самара": "Europe/Samara",
    "омск": "Asia/Omsk",
    "томск": "Asia/Tomsk",
    "перм": "Asia/Yekaterinburg",
    "казань": "Europe/Moscow",
    "ростов": "Europe/Moscow",
    "краснодар": "Europe/Moscow",
}


@dataclass
class TimezoneResolution:
    timezone: str
    source: str
    warning: str | None = None


class DealTimezoneResolver:
    def __init__(self, db: Session, portal_id: str, fallback: str = "Europe/Moscow"):
        self.db = db
        self.portal_id = portal_id
        self.fallback = fallback

    def resolve_for_deal(self, deal_local_id: int | None) -> TimezoneResolution:
        if deal_local_id is None:
            return TimezoneResolution(
                timezone=self.fallback,
                source="fallback",
                warning="Timezone was not determined from CRM data",
            )
        deal = self.db.get(CrmEntity, deal_local_id)
        if deal is None:
            return TimezoneResolution(
                timezone=self.fallback,
                source="fallback",
                warning="Timezone was not determined from CRM data",
            )
        region = self._read_region(deal)
        if not region:
            return TimezoneResolution(
                timezone=self.fallback,
                source="fallback",
                warning="Timezone was not determined from CRM data",
            )
        tz = self._region_to_tz(region)
        return TimezoneResolution(timezone=tz, source=f"region:{region}")

    def _read_region(self, deal: CrmEntity) -> str | None:
        payload = deal.raw_payload or {}
        for key in (TOMORU_REGION_FIELD, camel_key(TOMORU_REGION_FIELD)):
            val = payload.get(key)
            if val not in (None, ""):
                return str(val).strip()

        fv = self.db.scalar(
            select(CrmEntityFieldValue)
            .join(
                CrmFieldDefinition,
                CrmFieldDefinition.id == CrmEntityFieldValue.field_definition_id,
            )
            .where(
                CrmEntityFieldValue.portal_id == self.portal_id,
                CrmEntityFieldValue.entity_type_id == deal.entity_type_id,
                CrmEntityFieldValue.entity_id == deal.entity_id,
                CrmEntityFieldValue.is_current.is_(True),
                CrmFieldDefinition.original_field_name == TOMORU_REGION_FIELD,
            )
        )
        if fv is None or not fv.text_value:
            return None
        return str(fv.text_value).strip()

    @staticmethod
    def _region_to_tz(region: str) -> str:
        low = region.lower()
        for key, tz in _REGION_TZ.items():
            if key in low:
                return tz
        return "Europe/Moscow"
