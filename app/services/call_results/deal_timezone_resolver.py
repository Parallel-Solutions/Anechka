"""Resolve deal timezone from CRM region field."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CrmDictionary,
    CrmDictionaryEntry,
    CrmEntity,
    CrmEntityFieldValue,
    CrmFieldDefinition,
)
from app.services.export_plan.payload_keys import camel_key
from app.services.intelligent_export.contact_phone_heuristic import TOMORU_REGION_FIELD

# Region name (lowercase substring) -> IANA timezone
_REGION_ID_TZ: dict[str, str] = {
    "1105": "Europe/Moscow",       # Москва
    "1107": "Europe/Moscow",       # Санкт-Петербург
    "1091": "Asia/Tomsk",          # Томск
    "1089": "Europe/Moscow",       # Тверь
    "1007": "Asia/Yakutsk",        # Амурская область
}

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
    "архангельск": "Europe/Moscow",
    "белгород": "Europe/Moscow",
    "брянск": "Europe/Moscow",
    "владимир": "Europe/Moscow",
    "волгоград": "Europe/Moscow",
    "вологод": "Europe/Moscow",
    "воронеж": "Europe/Moscow",
    "дагестан": "Europe/Moscow",
    "иванов": "Europe/Moscow",
    "кабардино": "Europe/Moscow",
    "калмык": "Europe/Moscow",
    "калуж": "Europe/Moscow",
    "карачаево": "Europe/Moscow",
    "карел": "Europe/Moscow",
    "киров": "Europe/Moscow",
    "коми": "Europe/Moscow",
    "костром": "Europe/Moscow",
    "курск": "Europe/Moscow",
    "липецк": "Europe/Moscow",
    "марий": "Europe/Moscow",
    "мордов": "Europe/Moscow",
    "мурман": "Europe/Moscow",
    "нижегород": "Europe/Moscow",
    "новгород": "Europe/Moscow",
    "орлов": "Europe/Moscow",
    "пенз": "Europe/Moscow",
    "псков": "Europe/Moscow",
    "рязан": "Europe/Moscow",
    "северная осет": "Europe/Moscow",
    "смолен": "Europe/Moscow",
    "ставропол": "Europe/Moscow",
    "тамбов": "Europe/Moscow",
    "твер": "Europe/Moscow",
    "туль": "Europe/Moscow",
    "чечен": "Europe/Moscow",
    "чуваш": "Europe/Moscow",
    "ярослав": "Europe/Moscow",
    "астрахан": "Europe/Samara",
    "саратов": "Europe/Samara",
    "ульянов": "Europe/Samara",
    "удмурт": "Europe/Samara",
    "башкортостан": "Asia/Yekaterinburg",
    "курган": "Asia/Yekaterinburg",
    "оренбург": "Asia/Yekaterinburg",
    "тюмен": "Asia/Yekaterinburg",
    "челябин": "Asia/Yekaterinburg",
    "ханты-мансий": "Asia/Yekaterinburg",
    "ямало-ненец": "Asia/Yekaterinburg",
    "алтай": "Asia/Krasnoyarsk",
    "кемеров": "Asia/Krasnoyarsk",
    "тыва": "Asia/Krasnoyarsk",
    "хакас": "Asia/Krasnoyarsk",
    "бурят": "Asia/Irkutsk",
    "амур": "Asia/Yakutsk",
    "забайкал": "Asia/Yakutsk",
    "саха": "Asia/Yakutsk",
    "якут": "Asia/Yakutsk",
    "примор": "Asia/Vladivostok",
    "хабаров": "Asia/Vladivostok",
    "еврейск": "Asia/Vladivostok",
    "магадан": "Asia/Magadan",
    "сахалин": "Asia/Sakhalin",
    "камчат": "Asia/Kamchatka",
    "чукот": "Asia/Anadyr",
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
                raw_region = str(val).strip()
                return self._dictionary_region_name(raw_region) or raw_region

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
        if fv is None:
            return None
        if fv.dictionary_entry_id:
            entry = self.db.get(CrmDictionaryEntry, int(fv.dictionary_entry_id))
            if entry is not None:
                return str(entry.normalized_value or entry.raw_value or entry.external_id).strip()
        if not fv.text_value:
            return None
        raw_region = str(fv.text_value).strip()
        return self._dictionary_region_name(raw_region) or raw_region

    def _dictionary_region_name(self, external_id: str) -> str | None:
        entry = self.db.scalar(
            select(CrmDictionaryEntry)
            .join(CrmDictionary, CrmDictionary.id == CrmDictionaryEntry.dictionary_id)
            .where(
                CrmDictionary.portal_id == self.portal_id,
                CrmDictionaryEntry.external_id == external_id,
                CrmDictionaryEntry.is_active.is_(True),
            )
        )
        if entry is None:
            return None
        return str(entry.normalized_value or entry.raw_value or entry.external_id).strip()

    @staticmethod
    def _region_to_tz(region: str) -> str:
        direct = _REGION_ID_TZ.get(region.strip())
        if direct:
            return direct
        low = region.lower()
        for key, tz in _REGION_TZ.items():
            if key in low:
                return tz
        return "Europe/Moscow"
