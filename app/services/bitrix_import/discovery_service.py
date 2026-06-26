"""Schema and dictionary discovery for Bitrix CRM fields."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.repositories.crm_repository import CrmRepository
from app.services.bitrix_import.bitrix_crm_client import BitrixCrmClient

logger = logging.getLogger(__name__)

ENTITY_TYPE_IDS = [1, 2, 3, 4]


class SchemaDiscoveryService:
    def __init__(
        self,
        db: Session,
        portal_id: str,
        client: BitrixCrmClient,
        sync_run_id: int | None = None,
    ):
        self.db = db
        self.portal_id = portal_id
        self.client = client
        self.sync_run_id = sync_run_id
        self.repo = CrmRepository(db, portal_id)

    def discover_all_fields(self) -> dict[int, list[CrmRepository]]:
        results: dict[int, list] = {}
        for etype in ENTITY_TYPE_IDS:
            results[etype] = self.discover_fields(etype)
        return results

    def discover_fields(self, entity_type_id: int) -> list:
        raw_fields = self.client.get_item_fields(entity_type_id)
        seen: set[str] = set()
        definitions = []
        for name, meta in raw_fields.items():
            if not isinstance(meta, dict):
                meta = {"type": "string", "title": str(meta)}
            meta = dict(meta)
            meta["code"] = name
            original = meta.get("upperName") or name
            if name.startswith("UF_CRM_") or name.startswith("ufCrm"):
                original = name if name.startswith("UF_CRM_") else name
            field_def, changed = self.repo.upsert_field_definition(
                entity_type_id, original, meta, self.sync_run_id
            )
            seen.add(field_def.original_field_name)
            definitions.append(field_def)
            ftype = str(meta.get("type", ""))
            if ftype and ftype not in (
                "string", "integer", "double", "boolean", "datetime", "date",
                "enumeration", "crm_status", "crm_category", "crm_currency",
                "user", "crm", "file", "url", "money", "address",
            ):
                if ftype not in self.client.diagnostics.unknown_field_types:
                    self.client.diagnostics.unknown_field_types.append(ftype)
        deactivated = self.repo.deactivate_missing_fields(entity_type_id, seen)
        if deactivated:
            logger.info("Deactivated %s fields for entity type %s", deactivated, entity_type_id)
        return definitions

    def discover_dictionaries(self, entity_type_id: int) -> None:
        fields = self.client.get_item_fields(entity_type_id)
        for name, meta in fields.items():
            if not isinstance(meta, dict):
                continue
            ftype = str(meta.get("type", ""))
            if ftype == "enumeration":
                self._sync_enumeration(entity_type_id, name, meta)
            elif ftype in ("crm_status", "crm"):
                self._sync_status_field(entity_type_id, name, meta)

        if entity_type_id == 2:
            for cat in self.client.list_all_categories(2):
                d = self.repo.upsert_dictionary(
                    entity_type_id,
                    f"category_{cat.get('id')}",
                    "crm.category",
                    title=cat.get("name"),
                )
                self.repo.upsert_dictionary_entry(
                    d.id, str(cat.get("id")), cat.get("name"), raw_payload=cat
                )
            for cat in self.client.list_all_categories(2):
                cat_id = int(cat.get("id") or 0)
                entity_id = "DEAL_STAGE" if cat_id == 0 else f"DEAL_STAGE_{cat_id}"
                self._sync_deal_stage_entity(entity_type_id, entity_id)

    def _sync_deal_stage_entity(self, entity_type_id: int, entity_id: str) -> None:
        d = self.repo.upsert_dictionary(
            entity_type_id,
            f"status_{entity_id}",
            "crm.status",
            title=entity_id,
        )
        seen: set[str] = set()
        for status in self.client.list_statuses(entity_id):
            sid = str(status.get("STATUS_ID", ""))
            if not sid:
                continue
            seen.add(sid)
            self.repo.upsert_dictionary_entry(
                d.id, sid, status.get("NAME"), raw_payload=status
            )
        self.repo.deactivate_dictionary_entries(d.id, seen)

    def _sync_enumeration(self, entity_type_id: int, field_name: str, meta: dict) -> None:
        field_def, _ = self.repo.upsert_field_definition(
            entity_type_id, field_name, meta, self.sync_run_id
        )
        d = self.repo.upsert_dictionary(
            entity_type_id,
            f"enum_{entity_type_id}_{field_name}",
            "Bitrix enumeration",
            field_definition_id=field_def.id,
            title=meta.get("title") or field_name,
        )
        items = meta.get("items") or meta.get("settings", {}).get("items") or []
        seen: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                eid = str(item.get("ID") or item.get("id") or item.get("VALUE"))
                val = str(item.get("VALUE") or item.get("value") or "")
            else:
                eid = str(item)
                val = str(item)
            seen.add(eid)
            self.repo.upsert_dictionary_entry(d.id, eid, val, raw_payload=item if isinstance(item, dict) else None)
        self.repo.deactivate_dictionary_entries(d.id, seen)

    def _sync_status_field(self, entity_type_id: int, field_name: str, meta: dict) -> None:
        settings = meta.get("settings") or {}
        entity_id = settings.get("entityId") or settings.get("ENTITY_ID")
        if not entity_id:
            return
        d = self.repo.upsert_dictionary(
            entity_type_id,
            f"status_{entity_id}",
            "crm.status",
            title=field_name,
        )
        seen: set[str] = set()
        for status in self.client.list_statuses(str(entity_id)):
            sid = str(status.get("STATUS_ID", ""))
            seen.add(sid)
            self.repo.upsert_dictionary_entry(
                d.id, sid, status.get("NAME"), raw_payload=status
            )
        self.repo.deactivate_dictionary_entries(d.id, seen)

    def sync_global_dictionaries(self) -> None:
        users = self.client.get_users()
        d_users = self.repo.upsert_dictionary(0, "users", "user", title="Пользователи")
        seen_users: set[str] = set()
        for u in users:
            uid = str(u["id"])
            seen_users.add(uid)
            self.repo.upsert_user(u["id"], u["name"], u)
            self.repo.upsert_dictionary_entry(d_users.id, uid, u["name"])
        self.repo.deactivate_dictionary_entries(d_users.id, seen_users)

        d_curr = self.repo.upsert_dictionary(0, "currencies", "currency", title="Валюты")
        seen_curr: set[str] = set()
        for c in self.client.list_currencies():
            code = str(c.get("CURRENCY") or c.get("currency") or c.get("code", ""))
            if not code:
                continue
            seen_curr.add(code)
            self.repo.upsert_dictionary_entry(d_curr.id, code, c.get("FULL_NAME") or code, raw_payload=c)
        self.repo.deactivate_dictionary_entries(d_curr.id, seen_curr)
