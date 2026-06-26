"""Import orchestration for Bitrix CRM data."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CrmFieldDefinition, CrmContactLink, ENTITY_CONTACT, ENTITY_DEAL, ENTITY_LEAD, utcnow
from app.repositories.crm_repository import CrmRepository
from app.repositories.sync_repository import SyncRepository
from app.services.bitrix_import.bitrix_crm_client import BitrixCrmClient
from app.services.bitrix_import.contact_enrichment_service import ContactEnrichmentService
from app.services.bitrix_import.discovery_service import SchemaDiscoveryService
from app.services.bitrix_import.field_value_profiler import FieldValueProfiler
from app.services.bitrix_import.file_storage import get_file_storage
from app.services.bitrix_import.metadata_ai_service import BitrixMetadataAIService
from app.services.bitrix_client import BATCH_SIZE
from app.utils.datetime_utils import parse_bitrix_datetime

logger = logging.getLogger(__name__)

PRIMARY_ENTITY_TYPES = [ENTITY_LEAD, ENTITY_DEAL]
RELATED_ENTITY_TYPES = [3, 4]  # contact, company


class ImportOrchestrator:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        portal_id: str,
        sync_run_id: int,
        cancel_check: Callable[[], bool] | None = None,
        progress_callback: Callable[..., None] | None = None,
    ):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id
        self.sync_run_id = sync_run_id
        self.cancel_check = cancel_check or (lambda: False)
        self.progress_callback = progress_callback
        self.sync_repo = SyncRepository(db)
        self.crm_repo = CrmRepository(db, portal_id)
        self.client = BitrixCrmClient(settings, cancel_check)
        self.discovery = SchemaDiscoveryService(db, portal_id, self.client, sync_run_id)
        self.ai_service = BitrixMetadataAIService(settings, db, portal_id)
        self.profiler = FieldValueProfiler(db, portal_id)
        self.file_storage = get_file_storage(settings)
        self.touched: dict[int, set[int]] = {ENTITY_LEAD: set(), ENTITY_DEAL: set()}
        self.enrichment = ContactEnrichmentService(db, portal_id, self.client)
        self.stats = {
            "processed": 0,
            "created": 0,
            "updated": 0,
            "unchanged": 0,
            "deleted": 0,
            "failed": 0,
            "links_created": 0,
            "contacts_synced": 0,
        }

    def run(self, mode: str, analyze_metadata: bool = True) -> None:
        run = self.sync_repo.get_run(self.sync_run_id)
        if not run:
            raise ValueError("Sync run not found")

        if mode == "full" or self._is_first_import():
            self._run_full(analyze_metadata)
        elif mode == "incremental":
            self._run_incremental(analyze_metadata)
        elif mode == "reconciliation":
            self._run_reconciliation()
        elif mode == "schema_only":
            self._run_schema_only()
        elif mode == "ai_reanalysis":
            self._update_run("value_profiling")
            self.profiler.profile_all()
            self.db.commit()
            self._update_run("ai_analysis")
            self._run_ai_reanalysis(force=True)
            self._update_run("completed")
        elif mode == "contacts_backfill":
            self._run_contacts_backfill()
        else:
            raise ValueError(f"Unknown mode: {mode}")

    def _is_first_import(self) -> bool:
        cp = self.sync_repo.get_checkpoint(self.portal_id, "entities", ENTITY_LEAD)
        return cp is None or cp.cursor_time is None

    def _update_run(self, phase: str) -> None:
        self.sync_repo.update_progress(
            self.sync_run_id,
            current_phase=phase,
            processed_count=self.stats["processed"],
            created_count=self.stats["created"],
            updated_count=self.stats["updated"],
            unchanged_count=self.stats["unchanged"],
            deleted_count=self.stats["deleted"],
            failed_count=self.stats["failed"],
            api_requests_count=self.client.api_requests_count,
            ai_requests_count=self.ai_service.ai_requests_count,
            heartbeat_at=utcnow(),
        )
        if self.progress_callback:
            self.progress_callback(phase, self.stats)

    def _check_cancel(self) -> bool:
        run = self.sync_repo.get_run(self.sync_run_id)
        return bool(run and run.cancel_requested)

    def _run_schema_only(self) -> None:
        self._update_run("schema")
        for etype in PRIMARY_ENTITY_TYPES + RELATED_ENTITY_TYPES:
            self.discovery.discover_fields(etype)
            self.discovery.discover_dictionaries(etype)
        self.discovery.sync_global_dictionaries()
        self.db.commit()
        self._update_run("completed")

    def _run_full(self, analyze_metadata: bool) -> None:
        snapshot = utcnow()
        self._update_run("schema")
        for etype in PRIMARY_ENTITY_TYPES + RELATED_ENTITY_TYPES:
            if self._check_cancel():
                return
            self.discovery.discover_fields(etype)
            self.discovery.discover_dictionaries(etype)
        self.discovery.sync_global_dictionaries()
        self.db.commit()

        self._update_run("entities")
        for etype in PRIMARY_ENTITY_TYPES:
            if self._check_cancel():
                return
            self._import_entities_full(etype)

        self._update_run("related")
        self._import_related_entities()

        self._update_run("contacts")
        self._enrich_touched()
        self.db.commit()

        self._update_run("overlap")
        self._import_overlap(snapshot)

        self._update_run("value_profiling")
        self.profiler.profile_all()
        self.db.commit()

        if analyze_metadata:
            self._update_run("ai_analysis")
            self._run_ai_reanalysis(force=False)

        self._update_run("checkpoints")
        self._save_checkpoints()
        self.db.commit()

    def _run_incremental(self, analyze_metadata: bool) -> None:
        self._update_run("schema_check")
        for etype in PRIMARY_ENTITY_TYPES:
            self.discovery.discover_fields(etype)

        self._update_run("entities")
        for etype in PRIMARY_ENTITY_TYPES:
            if self._check_cancel():
                return
            self._import_entities_incremental(etype)

        self._update_run("related")
        self._import_related_entities()

        self._update_run("contacts")
        self._enrich_touched()
        self.db.commit()

        self._update_run("value_profiling")
        self.profiler.profile_all()
        self.db.commit()

        if analyze_metadata:
            self._update_run("ai_analysis")
            self._run_ai_reanalysis(force=False)

        self._update_run("checkpoints")
        self._save_checkpoints()
        self.db.commit()

    def _run_reconciliation(self) -> None:
        self._update_run("reconciliation")
        if self.client.diagnostics.permission_skips:
            logger.warning("Skipping mass deletion due to access issues")
            return
        for etype in PRIMARY_ENTITY_TYPES:
            remote_ids = set(self.client.list_all_item_ids(etype))
            local_entities = self.crm_repo.list_entities_paginated(
                etype, page=1, page_size=100000, is_deleted=False
            )[0]
            for entity in local_entities:
                if entity.entity_id not in remote_ids:
                    if self.crm_repo.mark_deleted(etype, entity.entity_id, self.sync_run_id):
                        self.stats["deleted"] += 1
            self.db.commit()
            cp = self.sync_repo.get_checkpoint(self.portal_id, "entities", etype)
            if cp:
                cp.last_reconciliation_at = utcnow()
        self._update_run("completed")

    def _run_contacts_backfill(self) -> None:
        self._update_run("schema")
        self.discovery.discover_fields(ENTITY_CONTACT)
        self.discovery.discover_dictionaries(ENTITY_CONTACT)
        self.discovery.sync_global_dictionaries()
        self.db.commit()

        self._update_run("contacts")
        self._enrich_all_primary()
        self.db.commit()
        self._update_run("completed")

    def _enrich_all_primary(self) -> None:
        page_size = self.settings.import_batch_size
        for etype in PRIMARY_ENTITY_TYPES:
            linked_ids = self._linked_parent_ids(etype)
            page = 1
            while True:
                if self._check_cancel():
                    return
                entities, total = self.crm_repo.list_entities_paginated(
                    etype, page=page, page_size=page_size, is_deleted=False
                )
                if not entities:
                    break

                to_enrich = [e for e in entities if e.entity_id not in linked_ids]
                if to_enrich:
                    ids = [e.entity_id for e in to_enrich]
                    payload_by_id = {e.entity_id: (e.raw_payload or {}) for e in to_enrich}

                    if etype == ENTITY_DEAL:
                        links_by_id = self.client.batch_deal_contacts(ids)
                    else:
                        links_by_id = self.client.batch_lead_contacts(ids)

                    contact_ids = self._collect_contact_ids_from_links(links_by_id)
                    for i in range(0, len(contact_ids), BATCH_SIZE):
                        self.client.prefetch_contacts_batch(contact_ids[i : i + BATCH_SIZE])

                    company_ids = self._collect_company_ids_from_contacts(contact_ids)
                    company_id_list = list(company_ids)
                    for i in range(0, len(company_id_list), BATCH_SIZE):
                        self.client.prefetch_companies_batch(company_id_list[i : i + BATCH_SIZE])

                    page_links = 0
                    for entity_id in ids:
                        try:
                            items = links_by_id.get(entity_id, [])
                            if etype == ENTITY_DEAL:
                                n = self.enrichment.enrich_deal_with_items(
                                    entity_id,
                                    items,
                                    payload_by_id.get(entity_id, {}),
                                )
                            else:
                                n = self.enrichment.enrich_lead_with_items(
                                    entity_id, items, payload_by_id.get(entity_id, {})
                                )
                            page_links += n
                            linked_ids.add(entity_id)
                            self.stats["processed"] += 1
                        except Exception:
                            logger.exception("Contact enrichment failed for %s/%s", etype, entity_id)
                            self.stats["failed"] += 1

                    self.stats["links_created"] += page_links
                    self.stats["contacts_synced"] += len(contact_ids)

                    self.db.commit()
                    self._update_run("contacts")
                else:
                    self._update_run("contacts")

                if page * page_size >= total:
                    break
                page += 1

    def _linked_parent_ids(self, entity_type_id: int) -> set[int]:
        rows = self.db.scalars(
            select(CrmContactLink.parent_entity_id).where(
                CrmContactLink.portal_id == self.portal_id,
                CrmContactLink.parent_entity_type_id == entity_type_id,
            ).distinct()
        )
        return set(rows)

    @staticmethod
    def _collect_contact_ids_from_links(links_by_id: dict[int, list[dict]]) -> list[int]:
        ids: set[int] = set()
        for items in links_by_id.values():
            for it in items:
                cid = it.get("CONTACT_ID") or it.get("contactId")
                if cid:
                    ids.add(int(cid))
        return sorted(ids)

    def _collect_company_ids_from_contacts(self, contact_ids: list[int]) -> set[int]:
        company_ids: set[int] = set()
        for cid in contact_ids:
            contact = self.client.contacts_cache.get(cid)
            if not contact:
                continue
            comp = contact.get("COMPANY_ID") or contact.get("companyId")
            if comp:
                company_ids.add(int(comp))
        return company_ids

    def _enrich_touched(self) -> None:
        for etype in (ENTITY_LEAD, ENTITY_DEAL):
            for entity_id in sorted(self.touched.get(etype, set())):
                if self._check_cancel():
                    return
                entity = self.crm_repo.get_entity(etype, entity_id)
                payload = entity.raw_payload if entity else {}
                self._enrich_one(etype, entity_id, payload or {})

    def _enrich_one(self, entity_type_id: int, entity_id: int, payload: dict) -> None:
        try:
            if entity_type_id == ENTITY_DEAL:
                self.enrichment.enrich_deal(entity_id, payload)
            else:
                self.enrichment.enrich_lead(entity_id, payload)
            self.stats["processed"] += 1
        except Exception:
            logger.exception("Contact enrichment failed for %s/%s", entity_type_id, entity_id)
            self.stats["failed"] += 1

    def _run_ai_reanalysis(self, force: bool = False) -> None:
        fields = list(
            self.db.query(CrmFieldDefinition)
            .filter(
                CrmFieldDefinition.portal_id == self.portal_id,
                CrmFieldDefinition.is_active.is_(True),
            )
            .all()
        )
        profiles = self.crm_repo.get_value_profiles_by_field_ids([f.id for f in fields])
        self.ai_service.analyze_fields(fields, value_profiles=profiles, force=force)
        self._update_run("ai_done")

    def _import_entities_full(self, entity_type_id: int) -> None:
        last_cursor_time: Any = None
        last_cursor_id: int | None = None
        for page in self.client.list_items_keyset(
            entity_type_id, batch_size=self.settings.import_batch_size
        ):
            if self._check_cancel():
                return
            self._process_entity_batch(entity_type_id, page)
            last_item = page[-1]
            last_cursor_time = parse_bitrix_datetime(str(last_item.get("updatedTime", "")))
            last_cursor_id = int(last_item.get("id") or 0)
            self.db.commit()
        if last_cursor_time:
            self.sync_repo.upsert_checkpoint(
                self.portal_id, "entities", entity_type_id, last_cursor_time, last_cursor_id
            )

    def _import_entities_incremental(self, entity_type_id: int) -> None:
        cp = self.sync_repo.get_checkpoint(self.portal_id, "entities", entity_type_id)
        cursor_time = None
        cursor_id = None
        if cp and cp.cursor_time:
            cursor_time = cp.cursor_time - timedelta(minutes=self.settings.import_overlap_minutes)
            cursor_id = cp.cursor_id

        last_cursor_time = cp.cursor_time if cp else None
        last_cursor_id = cp.cursor_id if cp else None

        for page in self.client.list_items_keyset(
            entity_type_id,
            cursor_time=cursor_time,
            cursor_id=cursor_id or 0,
            batch_size=self.settings.import_batch_size,
        ):
            if self._check_cancel():
                return
            self._process_entity_batch(entity_type_id, page)
            last_item = page[-1]
            last_cursor_time = parse_bitrix_datetime(str(last_item.get("updatedTime", "")))
            last_cursor_id = int(last_item.get("id") or 0)
            self.db.commit()

        if last_cursor_time:
            self.sync_repo.upsert_checkpoint(
                self.portal_id, "entities", entity_type_id, last_cursor_time, last_cursor_id
            )

    def _process_entity_batch(self, entity_type_id: int, items: list[dict]) -> None:
        for item in items:
            entity_id = int(item.get("id") or item.get("ID") or 0)
            if not entity_id:
                continue
            try:
                _, action = self.crm_repo.upsert_entity(
                    entity_type_id, entity_id, item, self.sync_run_id
                )
                if entity_type_id in self.touched:
                    self.touched[entity_type_id].add(entity_id)
                self.stats["processed"] += 1
                self.stats[action] += 1
            except Exception as exc:
                logger.exception("Failed to upsert entity %s/%s", entity_type_id, entity_id)
                self.stats["failed"] += 1

    def _collect_related_ids(self) -> dict[int, set[int]]:
        related_ids: dict[int, set[int]] = {3: set(), 4: set()}
        for etype in PRIMARY_ENTITY_TYPES:
            entities, _ = self.crm_repo.list_entities_paginated(etype, page=1, page_size=100000)
            for entity in entities:
                payload = entity.raw_payload or {}
                contact_id = payload.get("contactId") or payload.get("CONTACT_ID")
                company_id = payload.get("companyId") or payload.get("COMPANY_ID")
                contact_ids = payload.get("contactIds")
                if contact_id:
                    related_ids[3].add(int(contact_id))
                if isinstance(contact_ids, list):
                    for cid in contact_ids:
                        if cid:
                            related_ids[3].add(int(cid))
                if company_id:
                    related_ids[4].add(int(company_id))
        return related_ids

    def _import_related_entities(self) -> None:
        related_ids = self._collect_related_ids()
        for etype in RELATED_ENTITY_TYPES:
            wanted = related_ids[etype]
            if not wanted:
                continue
            imported_ids: set[int] = set()
            for page in self.client.list_items_keyset(
                etype, batch_size=self.settings.import_batch_size
            ):
                if self._check_cancel():
                    return
                batch = [
                    item
                    for item in page
                    if int(item.get("id") or item.get("ID") or 0) in wanted
                ]
                if batch:
                    self._process_entity_batch(etype, batch)
                    imported_ids.update(
                        int(item.get("id") or item.get("ID") or 0) for item in batch
                    )
                self.db.commit()
                self._update_run("related")
            missing = wanted - imported_ids
            for eid in sorted(missing):
                msg = f"related entity {etype}/{eid}: not found in crm.item.list"
                if msg not in self.client.diagnostics.permission_skips:
                    self.client.diagnostics.permission_skips.append(msg)

    def _import_overlap(self, snapshot_started_at: Any) -> None:
        overlap_start = snapshot_started_at - timedelta(minutes=self.settings.import_overlap_minutes)
        for etype in PRIMARY_ENTITY_TYPES:
            for page in self.client.list_items_keyset(
                etype,
                cursor_time=overlap_start,
                cursor_id=0,
                batch_size=self.settings.import_batch_size,
                filter_extra={">=updatedTime": overlap_start.isoformat()},
            ):
                self._process_entity_batch(etype, page)
                self.db.commit()

    def _save_checkpoints(self) -> None:
        for etype in PRIMARY_ENTITY_TYPES:
            cp = self.sync_repo.get_checkpoint(self.portal_id, "entities", etype)
            if cp:
                continue
            self.sync_repo.upsert_checkpoint(self.portal_id, "entities", etype, utcnow(), 0)

    def get_diagnostics(self) -> dict:
        return self.client.diagnostics.to_dict()
