"""Repository for CRM entities and related data."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    CrmChildRecord,
    CrmDictionary,
    CrmDictionaryEntry,
    CrmEntity,
    CrmEntityFieldValue,
    CrmEntityVersion,
    CrmFieldDefinition,
    CrmFieldDefinitionVersion,
    CrmFieldSemantic,
    CrmFieldValueProfile,
    CrmFile,
    CrmUser,
    ENTITY_DEAL,
    utcnow,
)
from app.services.intelligent_export.kp_legacy_stages import legacy_kp_stage_ids
from app.utils.datetime_utils import parse_bitrix_datetime
from app.utils.hash_utils import definition_hash, payload_hash


ENTITY_KIND_MAP = {1: "lead", 2: "deal", 3: "contact", 4: "company"}


class CrmRepository:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id

    def upsert_entity(
        self,
        entity_type_id: int,
        entity_id: int,
        raw_payload: dict[str, Any],
        sync_run_id: int | None = None,
    ) -> tuple[CrmEntity, str]:
        """Returns (entity, action) where action is created|updated|unchanged."""
        phash = payload_hash(raw_payload)
        existing = self.db.scalar(
            select(CrmEntity).where(
                CrmEntity.portal_id == self.portal_id,
                CrmEntity.entity_type_id == entity_type_id,
                CrmEntity.entity_id == entity_id,
            )
        )
        now = utcnow()
        action = "unchanged"

        if existing is None:
            existing = CrmEntity(
                portal_id=self.portal_id,
                entity_type_id=entity_type_id,
                entity_id=entity_id,
                entity_kind=ENTITY_KIND_MAP.get(entity_type_id, "unknown"),
                raw_payload=raw_payload,
                payload_hash=phash,
                first_imported_at=now,
            )
            self._fill_entity_columns(existing, raw_payload)
            self.db.add(existing)
            action = "created"
            self._create_version(existing, phash, raw_payload, sync_run_id)
        else:
            deleted_changed = existing.is_deleted and not self._is_deleted_payload(raw_payload)
            if existing.payload_hash != phash or deleted_changed:
                if existing.is_deleted:
                    existing.is_deleted = False
                    existing.deleted_at = None
                existing.raw_payload = raw_payload
                existing.payload_hash = phash
                self._fill_entity_columns(existing, raw_payload)
                existing.last_imported_at = now
                action = "updated"
                self._close_current_version(entity_type_id, entity_id)
                self._create_version(existing, phash, raw_payload, sync_run_id)
            else:
                existing.last_imported_at = now

        self.db.flush()
        return existing, action

    def mark_deleted(self, entity_type_id: int, entity_id: int, sync_run_id: int | None = None) -> bool:
        existing = self.db.scalar(
            select(CrmEntity).where(
                CrmEntity.portal_id == self.portal_id,
                CrmEntity.entity_type_id == entity_type_id,
                CrmEntity.entity_id == entity_id,
                CrmEntity.is_deleted.is_(False),
            )
        )
        if not existing:
            return False
        existing.is_deleted = True
        existing.deleted_at = utcnow()
        self._close_current_version(entity_type_id, entity_id)
        self._create_version(
            existing,
            payload_hash({**existing.raw_payload, "_deleted": True}),
            {**existing.raw_payload, "_deleted": True},
            sync_run_id,
            change_source="deletion",
        )
        return True

    def _fill_entity_columns(self, entity: CrmEntity, payload: dict[str, Any]) -> None:
        entity.title = str(payload.get("title") or payload.get("TITLE") or "") or None
        cat = payload.get("categoryId")
        if cat is None:
            cat = payload.get("CATEGORY_ID")
        if cat is not None and str(cat).isdigit():
            parsed = int(cat)
            entity.category_id = parsed if parsed > 0 else None
        else:
            entity.category_id = None
        entity.stage_id = str(payload.get("stageId") or payload.get("STAGE_ID") or "") or None
        legacy_stages = legacy_kp_stage_ids(self.db, self.portal_id)
        if (
            entity.entity_type_id == ENTITY_DEAL
            and entity.category_id is None
            and entity.stage_id
            and ":" not in entity.stage_id
            and entity.stage_id in legacy_stages
        ):
            from app.services.intelligent_export.kp_legacy_stages import KP_CATEGORY_ID

            entity.category_id = KP_CATEGORY_ID
        assigned = payload.get("assignedById") or payload.get("ASSIGNED_BY_ID")
        entity.assigned_by_id = int(assigned) if assigned else None
        entity.created_time = parse_bitrix_datetime(
            str(payload.get("createdTime") or payload.get("DATE_CREATE") or "")
        )
        entity.updated_time = parse_bitrix_datetime(
            str(payload.get("updatedTime") or payload.get("DATE_MODIFY") or "")
        )
        entity.closed_at = parse_bitrix_datetime(
            str(payload.get("closedate") or payload.get("CLOSEDATE") or "")
        )
        entity.source_id = str(payload.get("sourceId") or payload.get("SOURCE_ID") or "") or None
        entity.currency_id = str(payload.get("currencyId") or payload.get("CURRENCY_ID") or "") or None
        opp = payload.get("opportunity") or payload.get("OPPORTUNITY")
        entity.amount = float(opp) if opp not in (None, "") else None

    @staticmethod
    def _is_deleted_payload(payload: dict[str, Any]) -> bool:
        return bool(payload.get("_deleted"))

    def _create_version(
        self,
        entity: CrmEntity,
        phash: str,
        payload: dict,
        sync_run_id: int | None,
        change_source: str = "import",
    ) -> None:
        self.db.add(
            CrmEntityVersion(
                portal_id=self.portal_id,
                entity_type_id=entity.entity_type_id,
                entity_id=entity.entity_id,
                payload_hash=phash,
                raw_payload=payload,
                change_source=change_source,
                sync_run_id=sync_run_id,
            )
        )

    def _close_current_version(self, entity_type_id: int, entity_id: int) -> None:
        now = utcnow()
        self.db.execute(
            update(CrmEntityVersion)
            .where(
                CrmEntityVersion.portal_id == self.portal_id,
                CrmEntityVersion.entity_type_id == entity_type_id,
                CrmEntityVersion.entity_id == entity_id,
                CrmEntityVersion.valid_to.is_(None),
            )
            .values(valid_to=now)
        )

    def upsert_field_definition(
        self,
        entity_type_id: int,
        original_name: str,
        raw_def: dict[str, Any],
        sync_run_id: int | None = None,
    ) -> tuple[CrmFieldDefinition, bool]:
        dhash = definition_hash(raw_def)
        existing = self.db.scalar(
            select(CrmFieldDefinition).where(
                CrmFieldDefinition.portal_id == self.portal_id,
                CrmFieldDefinition.entity_type_id == entity_type_id,
                CrmFieldDefinition.original_field_name == original_name,
            )
        )
        changed = False
        now = utcnow()
        if existing is None:
            existing = CrmFieldDefinition(
                portal_id=self.portal_id,
                entity_type_id=entity_type_id,
                original_field_name=original_name,
                api_field_name=raw_def.get("code") or original_name,
                upper_name=original_name.upper(),
                title=raw_def.get("title") or raw_def.get("listLabel"),
                list_label=raw_def.get("listLabel"),
                form_label=raw_def.get("formLabel"),
                filter_label=raw_def.get("filterLabel"),
                field_type=str(raw_def.get("type") or ""),
                is_custom=original_name.startswith("UF_CRM_") or original_name.startswith("ufCrm"),
                is_multiple=bool(raw_def.get("isMultiple")),
                is_required=bool(raw_def.get("isRequired")),
                is_read_only=bool(raw_def.get("isReadOnly")),
                is_immutable=bool(raw_def.get("isImmutable")),
                settings=raw_def.get("settings"),
                raw_definition=raw_def,
                definition_hash=dhash,
                first_seen_at=now,
            )
            self.db.add(existing)
            self.db.flush()
            self._add_field_version(existing.id, dhash, raw_def, sync_run_id)
            changed = True
        else:
            existing.is_active = True
            existing.last_seen_at = now
            if existing.definition_hash != dhash:
                existing.definition_hash = dhash
                existing.raw_definition = raw_def
                existing.title = raw_def.get("title") or existing.title
                existing.field_type = str(raw_def.get("type") or existing.field_type)
                existing.settings = raw_def.get("settings")
                self._close_field_versions(existing.id)
                self._add_field_version(existing.id, dhash, raw_def, sync_run_id)
                changed = True
        return existing, changed

    def _add_field_version(
        self, field_id: int, dhash: str, raw_def: dict, sync_run_id: int | None
    ) -> None:
        self.db.add(
            CrmFieldDefinitionVersion(
                field_definition_id=field_id,
                definition_hash=dhash,
                raw_definition=raw_def,
                sync_run_id=sync_run_id,
            )
        )

    def _close_field_versions(self, field_id: int) -> None:
        now = utcnow()
        self.db.execute(
            update(CrmFieldDefinitionVersion)
            .where(
                CrmFieldDefinitionVersion.field_definition_id == field_id,
                CrmFieldDefinitionVersion.valid_to.is_(None),
            )
            .values(valid_to=now)
        )

    def deactivate_missing_fields(
        self, entity_type_id: int, seen_names: set[str]
    ) -> int:
        fields = list(
            self.db.scalars(
                select(CrmFieldDefinition).where(
                    CrmFieldDefinition.portal_id == self.portal_id,
                    CrmFieldDefinition.entity_type_id == entity_type_id,
                    CrmFieldDefinition.is_active.is_(True),
                )
            )
        )
        count = 0
        for f in fields:
            if f.discovered_from_payload:
                continue
            if f.original_field_name not in seen_names:
                f.is_active = False
                count += 1
        return count

    def upsert_dictionary(
        self,
        entity_type_id: int,
        dictionary_code: str,
        source_type: str,
        field_definition_id: int | None = None,
        title: str | None = None,
    ) -> CrmDictionary:
        existing = self.db.scalar(
            select(CrmDictionary).where(
                CrmDictionary.portal_id == self.portal_id,
                CrmDictionary.dictionary_code == dictionary_code,
                CrmDictionary.entity_type_id == entity_type_id,
            )
        )
        if existing:
            existing.is_active = True
            if title:
                existing.title = title
            return existing
        d = CrmDictionary(
            portal_id=self.portal_id,
            entity_type_id=entity_type_id,
            field_definition_id=field_definition_id,
            dictionary_code=dictionary_code,
            title=title,
            source_type=source_type,
        )
        self.db.add(d)
        self.db.flush()
        return d

    def upsert_dictionary_entry(
        self,
        dictionary_id: int,
        external_id: str,
        raw_value: str | None,
        raw_payload: dict | None = None,
    ) -> CrmDictionaryEntry:
        existing = self.db.scalar(
            select(CrmDictionaryEntry).where(
                CrmDictionaryEntry.dictionary_id == dictionary_id,
                CrmDictionaryEntry.external_id == external_id,
            )
        )
        if existing:
            existing.is_active = True
            existing.raw_value = raw_value
            existing.last_seen_at = utcnow()
            if raw_payload:
                existing.raw_payload = raw_payload
            return existing
        entry = CrmDictionaryEntry(
            dictionary_id=dictionary_id,
            external_id=external_id,
            raw_value=raw_value,
            raw_payload=raw_payload,
        )
        self.db.add(entry)
        return entry

    def deactivate_dictionary_entries(self, dictionary_id: int, seen_ids: set[str]) -> int:
        entries = list(
            self.db.scalars(
                select(CrmDictionaryEntry).where(
                    CrmDictionaryEntry.dictionary_id == dictionary_id,
                    CrmDictionaryEntry.is_active.is_(True),
                )
            )
        )
        count = 0
        for e in entries:
            if e.external_id not in seen_ids:
                e.is_active = False
                count += 1
        return count

    def upsert_child_record(
        self,
        record_type: str,
        external_id: str,
        raw_payload: dict,
        parent_entity_type_id: int | None = None,
        parent_entity_id: int | None = None,
    ) -> CrmChildRecord:
        phash = payload_hash(raw_payload)
        existing = self.db.scalar(
            select(CrmChildRecord).where(
                CrmChildRecord.portal_id == self.portal_id,
                CrmChildRecord.record_type == record_type,
                CrmChildRecord.external_id == external_id,
            )
        )
        if existing:
            if existing.payload_hash != phash:
                existing.raw_payload = raw_payload
                existing.payload_hash = phash
            existing.is_deleted = False
            existing.is_active = True
            existing.last_seen_at = utcnow()
            return existing
        rec = CrmChildRecord(
            portal_id=self.portal_id,
            record_type=record_type,
            external_id=external_id,
            parent_entity_type_id=parent_entity_type_id,
            parent_entity_id=parent_entity_id,
            raw_payload=raw_payload,
            payload_hash=phash,
        )
        self.db.add(rec)
        return rec

    def mark_child_records_deleted(
        self,
        record_type: str,
        parent_entity_type_id: int,
        parent_entity_id: int,
        seen_ids: set[str],
    ) -> int:
        records = list(
            self.db.scalars(
                select(CrmChildRecord).where(
                    CrmChildRecord.portal_id == self.portal_id,
                    CrmChildRecord.record_type == record_type,
                    CrmChildRecord.parent_entity_type_id == parent_entity_type_id,
                    CrmChildRecord.parent_entity_id == parent_entity_id,
                    CrmChildRecord.is_deleted.is_(False),
                )
            )
        )
        count = 0
        for r in records:
            if r.external_id not in seen_ids:
                r.is_deleted = True
                r.is_active = False
                count += 1
        return count

    def upsert_file_metadata(self, **kwargs: Any) -> CrmFile:
        bitrix_file_id = str(kwargs["bitrix_file_id"])
        existing = self.db.scalar(
            select(CrmFile).where(
                CrmFile.portal_id == self.portal_id,
                CrmFile.bitrix_file_id == bitrix_file_id,
            )
        )
        if existing:
            for k, v in kwargs.items():
                if hasattr(existing, k) and v is not None:
                    setattr(existing, k, v)
            existing.updated_at = utcnow()
            return existing
        f = CrmFile(portal_id=self.portal_id, **kwargs)
        self.db.add(f)
        return f

    def upsert_user(self, external_id: int, display_name: str, raw_payload: dict) -> CrmUser:
        phash = payload_hash(raw_payload)
        existing = self.db.scalar(
            select(CrmUser).where(
                CrmUser.portal_id == self.portal_id,
                CrmUser.external_id == external_id,
            )
        )
        if existing:
            existing.display_name = display_name
            existing.raw_payload = raw_payload
            existing.payload_hash = phash
            existing.last_seen_at = utcnow()
            return existing
        u = CrmUser(
            portal_id=self.portal_id,
            external_id=external_id,
            display_name=display_name,
            raw_payload=raw_payload,
            payload_hash=phash,
        )
        self.db.add(u)
        return u

    def count_entities(self, entity_type_id: int | None = None, is_deleted: bool | None = None) -> int:
        q = select(func.count()).select_from(CrmEntity).where(CrmEntity.portal_id == self.portal_id)
        if entity_type_id is not None:
            q = q.where(CrmEntity.entity_type_id == entity_type_id)
        if is_deleted is not None:
            q = q.where(CrmEntity.is_deleted.is_(is_deleted))
        return self.db.scalar(q) or 0

    def count_fields(self, custom_only: bool = False) -> int:
        q = select(func.count()).select_from(CrmFieldDefinition).where(
            CrmFieldDefinition.portal_id == self.portal_id,
            CrmFieldDefinition.is_active.is_(True),
        )
        if custom_only:
            q = q.where(CrmFieldDefinition.is_custom.is_(True))
        return self.db.scalar(q) or 0

    def get_semantic(self, field_definition_id: int) -> CrmFieldSemantic | None:
        return self.db.scalar(
            select(CrmFieldSemantic)
            .where(CrmFieldSemantic.field_definition_id == field_definition_id)
            .order_by(CrmFieldSemantic.generated_at.desc())
            .limit(1)
        )

    def get_value_profiles_by_field_ids(
        self, field_ids: list[int]
    ) -> dict[int, CrmFieldValueProfile]:
        if not field_ids:
            return {}
        rows = self.db.scalars(
            select(CrmFieldValueProfile).where(
                CrmFieldValueProfile.field_definition_id.in_(field_ids)
            )
        )
        return {r.field_definition_id: r for r in rows if r.field_definition_id is not None}

    def save_semantic(self, field_definition_id: int, data: dict[str, Any], **meta: Any) -> CrmFieldSemantic:
        existing = self.get_semantic(field_definition_id)
        if existing and existing.is_manual:
            return existing
        merged = {**data, **meta}
        sem = CrmFieldSemantic(field_definition_id=field_definition_id, **merged)
        self.db.add(sem)
        return sem

    def list_entities_paginated(
        self,
        entity_type_id: int,
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        stage_id: str | None = None,
        category_id: int | None = None,
        assigned_by_id: int | None = None,
        is_deleted: bool | None = None,
        sort: str = "updated_time",
        order: str = "desc",
    ) -> tuple[list[CrmEntity], int]:
        q = select(CrmEntity).where(
            CrmEntity.portal_id == self.portal_id,
            CrmEntity.entity_type_id == entity_type_id,
        )
        if search:
            if search.isdigit():
                q = q.where(CrmEntity.entity_id == int(search))
            else:
                q = q.where(CrmEntity.title.ilike(f"%{search}%"))
        if stage_id:
            q = q.where(CrmEntity.stage_id == stage_id)
        if category_id is not None:
            q = q.where(CrmEntity.category_id == category_id)
        if assigned_by_id is not None:
            q = q.where(CrmEntity.assigned_by_id == assigned_by_id)
        if is_deleted is not None:
            q = q.where(CrmEntity.is_deleted.is_(is_deleted))

        total = self.db.scalar(select(func.count()).select_from(q.subquery())) or 0
        sort_col = getattr(CrmEntity, sort, CrmEntity.updated_time)
        q = q.order_by(sort_col.desc() if order == "desc" else sort_col.asc())
        q = q.offset((page - 1) * page_size).limit(page_size)
        return list(self.db.scalars(q)), total

    def get_entity(self, entity_type_id: int, entity_id: int) -> CrmEntity | None:
        return self.db.scalar(
            select(CrmEntity).where(
                CrmEntity.portal_id == self.portal_id,
                CrmEntity.entity_type_id == entity_type_id,
                CrmEntity.entity_id == entity_id,
            )
        )

    def get_entity_versions(self, entity_type_id: int, entity_id: int) -> list[CrmEntityVersion]:
        return list(
            self.db.scalars(
                select(CrmEntityVersion)
                .where(
                    CrmEntityVersion.portal_id == self.portal_id,
                    CrmEntityVersion.entity_type_id == entity_type_id,
                    CrmEntityVersion.entity_id == entity_id,
                )
                .order_by(CrmEntityVersion.valid_from.desc())
            )
        )

    def get_child_records(
        self, parent_entity_type_id: int, parent_entity_id: int, record_type: str | None = None
    ) -> list[CrmChildRecord]:
        q = select(CrmChildRecord).where(
            CrmChildRecord.portal_id == self.portal_id,
            CrmChildRecord.parent_entity_type_id == parent_entity_type_id,
            CrmChildRecord.parent_entity_id == parent_entity_id,
            CrmChildRecord.is_deleted.is_(False),
        )
        if record_type:
            q = q.where(CrmChildRecord.record_type == record_type)
        return list(self.db.scalars(q))

    def replace_field_values(
        self,
        entity_type_id: int,
        entity_id: int,
        field_definition_id: int,
        values: list[dict[str, Any]],
    ) -> None:
        now = utcnow()
        self.db.execute(
            update(CrmEntityFieldValue)
            .where(
                CrmEntityFieldValue.portal_id == self.portal_id,
                CrmEntityFieldValue.entity_type_id == entity_type_id,
                CrmEntityFieldValue.entity_id == entity_id,
                CrmEntityFieldValue.field_definition_id == field_definition_id,
                CrmEntityFieldValue.is_current.is_(True),
            )
            .values(is_current=False, valid_to=now)
        )
        for idx, val in enumerate(values):
            self.db.add(
                CrmEntityFieldValue(
                    portal_id=self.portal_id,
                    entity_type_id=entity_type_id,
                    entity_id=entity_id,
                    field_definition_id=field_definition_id,
                    value_index=idx,
                    raw_value=val.get("raw"),
                    text_value=val.get("text"),
                    numeric_value=val.get("numeric"),
                    boolean_value=val.get("boolean"),
                    date_value=val.get("date"),
                    datetime_value=val.get("datetime"),
                    dictionary_entry_id=val.get("dictionary_entry_id"),
                    related_entity_type_id=val.get("related_entity_type_id"),
                    related_entity_id=val.get("related_entity_id"),
                    value_hash=payload_hash(val),
                    is_current=True,
                )
            )
