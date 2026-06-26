"""Накопление профиля значений по каждому полю CRM из raw_payload."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CrmEntity, CrmFieldDefinition, CrmFieldValueProfile, utcnow
from app.utils.anonymize import anonymize_string, numeric_stats, string_stats
from app.utils.hash_utils import source_hash

logger = logging.getLogger(__name__)

ENTITY_TYPE_IDS = [1, 2, 3, 4]
SAMPLE_CAP = 50
DISTINCT_SCAN_CAP = 300
PAGE_SIZE = 500


def _flatten_value(value: Any) -> str | None:
    """Приводит значение поля к строке для статистики, либо None если пусто."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        s = value.strip()
        return s or None
    if isinstance(value, (list, dict)):
        return None if not value else str(value)[:160]
    return str(value)


def _infer_type(values: list[Any]) -> str:
    for v in values:
        if isinstance(v, bool):
            return "boolean"
        if isinstance(v, (int, float)):
            return "number"
        if isinstance(v, list):
            return "array"
        if isinstance(v, dict):
            return "object"
    return "string"


class FieldValueProfiler:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id

    def profile_all(self) -> int:
        """Профилирует все типы сущностей. Возвращает число обновлённых профилей."""
        updated = 0
        for etype in ENTITY_TYPE_IDS:
            updated += self.profile_entity_type(etype)
        self.db.flush()
        return updated

    def profile_entity_type(self, entity_type_id: int) -> int:
        filled: dict[str, int] = {}
        nulls: dict[str, int] = {}
        total = 0
        samples: dict[str, dict[str, None]] = {}
        raw_examples: dict[str, list[Any]] = {}
        numeric_vals: dict[str, list[float]] = {}

        offset = 0
        while True:
            rows = list(
                self.db.scalars(
                    select(CrmEntity)
                    .where(
                        CrmEntity.portal_id == self.portal_id,
                        CrmEntity.entity_type_id == entity_type_id,
                        CrmEntity.is_deleted.is_(False),
                    )
                    .order_by(CrmEntity.id)
                    .offset(offset)
                    .limit(PAGE_SIZE)
                )
            )
            if not rows:
                break
            for ent in rows:
                total += 1
                payload = ent.raw_payload or {}
                if not isinstance(payload, dict):
                    continue
                for key, value in payload.items():
                    flat = _flatten_value(value)
                    if flat is None:
                        nulls[key] = nulls.get(key, 0) + 1
                        continue
                    filled[key] = filled.get(key, 0) + 1
                    raw_examples.setdefault(key, [])
                    if len(raw_examples[key]) < 5:
                        raw_examples[key].append(value)
                    bucket = samples.setdefault(key, {})
                    if len(bucket) < DISTINCT_SCAN_CAP:
                        bucket[flat] = None
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        numeric_vals.setdefault(key, []).append(float(value))
            offset += PAGE_SIZE

        updated = 0
        for key in set(list(filled.keys()) + list(nulls.keys())):
            f = filled.get(key, 0)
            n = nulls.get(key, 0)
            if f == 0:
                continue
            distinct_strings = list(samples.get(key, {}).keys())
            anon = [anonymize_string(s) for s in distinct_strings[:SAMPLE_CAP]]
            stats = string_stats(distinct_strings)
            nums = numeric_vals.get(key)
            num_stats = numeric_stats(nums) if nums else None
            otype = _infer_type(raw_examples.get(key, []))
            sig = source_hash(otype, f, len(distinct_strings), sorted(anon))

            fdef = self._ensure_field_definition(entity_type_id, key, otype)
            self._upsert_profile(
                entity_type_id=entity_type_id,
                field_code=key,
                field_definition_id=fdef.id if fdef else None,
                observed_types=[otype],
                total=total,
                filled=f,
                nulls=n,
                distinct=len(distinct_strings),
                samples=anon,
                length_stats=stats,
                numeric_stats=num_stats,
                signature=sig,
            )
            updated += 1
        return updated

    def _ensure_field_definition(
        self, entity_type_id: int, field_code: str, otype: str
    ) -> CrmFieldDefinition | None:
        existing = self.db.scalar(
            select(CrmFieldDefinition).where(
                CrmFieldDefinition.portal_id == self.portal_id,
                CrmFieldDefinition.entity_type_id == entity_type_id,
                CrmFieldDefinition.original_field_name == field_code,
            )
        )
        if existing:
            return existing
        fdef = CrmFieldDefinition(
            portal_id=self.portal_id,
            entity_type_id=entity_type_id,
            original_field_name=field_code,
            api_field_name=field_code,
            upper_name=field_code.upper(),
            title=None,
            field_type=otype,
            is_custom=field_code.startswith("UF_CRM_") or field_code.startswith("ufCrm"),
            discovered_from_payload=True,
            raw_definition={"code": field_code, "type": otype, "source": "payload"},
            definition_hash=source_hash("payload", field_code, otype),
            is_active=True,
        )
        self.db.add(fdef)
        self.db.flush()
        return fdef

    def _upsert_profile(self, **kw: Any) -> None:
        existing = self.db.scalar(
            select(CrmFieldValueProfile).where(
                CrmFieldValueProfile.portal_id == self.portal_id,
                CrmFieldValueProfile.entity_type_id == kw["entity_type_id"],
                CrmFieldValueProfile.field_code == kw["field_code"],
            )
        )
        if existing is None:
            existing = CrmFieldValueProfile(
                portal_id=self.portal_id,
                entity_type_id=kw["entity_type_id"],
                field_code=kw["field_code"],
                first_seen_at=utcnow(),
            )
            self.db.add(existing)
        existing.field_definition_id = kw["field_definition_id"]
        existing.observed_types = kw["observed_types"]
        existing.total_count = kw["total"]
        existing.filled_count = kw["filled"]
        existing.null_count = kw["nulls"]
        existing.distinct_count = kw["distinct"]
        existing.sample_values = kw["samples"]
        existing.length_stats = kw["length_stats"]
        existing.numeric_stats = kw["numeric_stats"]
        existing.value_signature = kw["signature"]
        existing.last_seen_at = utcnow()
