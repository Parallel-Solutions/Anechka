"""List deals for an export job — by filter criteria or from result file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppUser, CrmContact, CrmContactLink, CrmEntity, ExportJob, IeExportPlanVersion
from app.models.bitrix import ENTITY_COMPANY, ENTITY_DEAL
from app.repositories.contact_repository import ContactRepository
from app.repositories.crm_repository import CrmRepository
from app.services.auth_service import resolve_portal_id
from app.services.bitrix_client import BitrixClient
from app.services.export_plan.compiler_v2 import ExportPlanCompilerV2
from app.services.export_plan.models_v2 import Dataset
from app.services.export_plan.payload_keys import payload_lookup
from app.services.lpr_tomoru_service import _date_range_bounds
from app.services.intelligent_export.contact_lpr_classifier import build_lpr_classifier
from app.services.intelligent_export.contact_phone_heuristic import (
    CONFIDENCE_MANUAL,
    CONFIDENCE_SINGLE,
    _deal_company_id,
    _deal_only_contacts,
    collect_deal_contacts,
    filter_non_archived_deals,
    pick_company_phone,
    pick_contact_for_deal,
)
from app.services.lpr_pick_cache import (
    CachedLprPick,
    compute_input_hash,
    get_cached,
    load_all,
    save_all,
    set_cached,
)
from app.services.lpr_service import LprConfig, load_lpr_config
from app.services.tomoru_contact_preferences import load_all as load_tomoru_contact_overrides
from app.services.intelligent_export.plan_service import prepare_plan
from app.services.intelligent_export.scope import build_scope
from app.services.intelligent_export.tomoru_stages import (
    build_stage_names_map,
    resolve_archive_stage_ids,
)
from app.services.json_export_service import extract_deals_from_result
from app.utils.portal import bitrix_company_url, bitrix_contact_url, bitrix_deal_url

FILTER_NOTE = "Список по текущему импорту CRM (может отличаться от момента выгрузки)."
TOMORU_CONTACTS_NOTE = (
    "Отмечен контакт для выгрузки по эвристике (архитектор → ЛПР → последний). "
    "Можно выбрать несколько контактов и телефон компании. "
    "Выбор сохраняется для следующих выгрузок."
)
TRUNCATION_NOTE_TEMPLATE = (
    "По фильтрам найдено {matched_total} сделок. "
    "В выгрузку и превью попадёт не более {export_limit} — уточните фильтры или уменьшите выборку."
)

SAVED_SELECTION_REASON = "сохранённый выбор"


@dataclass
class ExportDealsResult:
    total: int
    deals: list[dict[str, Any]]
    available: bool
    source: str
    offset: int
    limit: int
    note: str | None = None
    matched_total: int | None = None
    truncated: bool = False
    lpr_summary: dict[str, Any] | None = None
    lpr_view: str = "all"


def _contact_to_preview(
    contact: CrmContact,
    link: CrmContactLink | None,
    source: str,
    portal_id: str,
    contact_repo: ContactRepository,
    *,
    selected_for_export: bool = False,
    selection_reason: str | None = None,
    selection_confidence: float | None = None,
) -> dict[str, Any]:
    raw = contact.raw_payload or {}
    description = str(payload_lookup(raw, "COMMENTS") or "").strip() or None
    post = str(contact.post or contact.post_custom or "").strip() or None
    phone = contact.primary_phone
    if not phone:
        phones = contact_repo.get_phones_for_contact(int(contact.contact_id))
        if phones:
            phone = phones[0].get("value")
    return {
        "contact_id": int(contact.contact_id),
        "full_name": contact.full_name or None,
        "post": post,
        "description": description,
        "phone": phone or None,
        "source": "company" if source == "company" else "deal",
        "is_primary": bool(link.is_primary) if link is not None else False,
        "selected_for_export": selected_for_export,
        "selection_reason": selection_reason,
        "selection_confidence": selection_confidence,
        "bitrix_url": bitrix_contact_url(portal_id, int(contact.contact_id)),
    }


def _company_for_deal(
    db: Session,
    portal_id: str,
    entity: CrmEntity,
    *,
    selected_for_export: bool = False,
) -> dict[str, Any] | None:
    company_id = _deal_company_id(entity)
    if company_id is None:
        return None
    company = CrmRepository(db, portal_id).get_entity(ENTITY_COMPANY, company_id)
    if company is None:
        return None
    raw = company.raw_payload or {}
    title = str(company.title or payload_lookup(raw, "TITLE") or "").strip() or None
    description = str(payload_lookup(raw, "COMMENTS") or "").strip() or None
    phone = pick_company_phone(db, portal_id, company_id)
    return {
        "company_id": company_id,
        "title": title,
        "phone": phone,
        "description": description,
        "bitrix_url": bitrix_company_url(portal_id, company_id),
        "selected_for_export": selected_for_export,
    }


def _contacts_for_deal(
    db: Session,
    portal_id: str,
    entity: CrmEntity,
    *,
    settings: Settings | None = None,
    lpr_config: LprConfig | None = None,
    saved_selection: list[int] | None = None,
    lpr_pick_cache: dict[int, CachedLprPick] | None = None,
    cache_dirty: list[bool] | None = None,
) -> list[dict[str, Any]]:
    contact_repo = ContactRepository(db, portal_id)
    candidates = collect_deal_contacts(
        db,
        portal_id,
        entity,
        include_company_contacts=True,
    )
    chosen_id: int | None = None
    selection_reason: str | None = None
    selection_confidence: float | None = None
    if saved_selection is not None:
        saved_ids = set(saved_selection)
        return [
            _contact_to_preview(
                c.contact,
                c.link,
                c.source,
                portal_id,
                contact_repo,
                selected_for_export=c.contact_id in saved_ids,
                selection_reason=SAVED_SELECTION_REASON if c.contact_id in saved_ids else None,
                selection_confidence=CONFIDENCE_MANUAL if c.contact_id in saved_ids else None,
            )
            for c in candidates
        ]
    if settings is not None and lpr_config is not None and candidates:
        deal_title = entity.title or ""
        input_hash = compute_input_hash(deal_title, candidates, lpr_config)
        deal_id = int(entity.entity_id)
        cached: CachedLprPick | None = None
        if lpr_pick_cache is not None:
            cached = get_cached(lpr_pick_cache, deal_id, input_hash)
        if cached is not None:
            chosen_id = cached.contact_id
            selection_reason = cached.reason
            selection_confidence = cached.confidence
        else:
            classifier = build_lpr_classifier(
                settings,
                lpr_config,
                use_llm=bool(settings.openai_api_key),
            )
            chosen, selection_reason, selection_confidence = pick_contact_for_deal(
                candidates,
                lpr_config=lpr_config,
                classifier=classifier,
                deal_title=deal_title,
            )
            if chosen is not None:
                chosen_id = chosen.contact_id
            if (
                lpr_pick_cache is not None
                and chosen_id is not None
                and selection_reason is not None
                and selection_confidence is not None
            ):
                set_cached(
                    lpr_pick_cache,
                    deal_id,
                    input_hash=input_hash,
                    contact_id=chosen_id,
                    reason=selection_reason,
                    confidence=selection_confidence,
                )
                if cache_dirty is not None:
                    cache_dirty[0] = True

        deal_contacts = _deal_only_contacts(candidates)
        if (
            len(deal_contacts) == 1
            and chosen_id is not None
            and chosen_id == deal_contacts[0].contact_id
        ):
            selection_confidence = CONFIDENCE_SINGLE

    return [
        _contact_to_preview(
            c.contact,
            c.link,
            c.source,
            portal_id,
            contact_repo,
            selected_for_export=chosen_id is not None and c.contact_id == chosen_id,
            selection_reason=selection_reason if chosen_id is not None and c.contact_id == chosen_id else None,
            selection_confidence=(
                selection_confidence if chosen_id is not None and c.contact_id == chosen_id else None
            ),
        )
        for c in candidates
    ]


def _deal_lpr_confidence(contacts: list[dict[str, Any]] | None) -> float | None:
    if not contacts:
        return None
    for c in contacts:
        if c.get("selected_for_export"):
            return c.get("selection_confidence")
    return None


def _is_uncertain_deal(confidence: float | None, threshold: float) -> bool:
    if confidence is None:
        return True
    return confidence < threshold


def _compute_lpr_summary(deals: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    deals_total = len(deals)
    if deals_total == 0:
        return {
            "threshold": threshold,
            "deals_total": 0,
            "confident_count": 0,
            "confident_percent": 0.0,
            "uncertain_count": 0,
        }
    confident_count = sum(
        1
        for deal in deals
        if (conf := deal.get("lpr_confidence")) is not None and conf >= threshold
    )
    uncertain_count = deals_total - confident_count
    confident_percent = round(confident_count / deals_total * 100, 1)
    return {
        "threshold": threshold,
        "deals_total": deals_total,
        "confident_count": confident_count,
        "confident_percent": confident_percent,
        "uncertain_count": uncertain_count,
    }


def _entity_to_deal(
    entity: CrmEntity | None,
    portal_id: str = "default",
    *,
    contacts: list[dict[str, Any]] | None = None,
    company: dict[str, Any] | None = None,
    stage_names: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if entity is None:
        return None
    stage_id = entity.stage_id
    stage_name: str | None = None
    if stage_id:
        stage_name = (stage_names or {}).get(stage_id, stage_id)
    deal: dict[str, Any] = {
        "deal_id": entity.entity_id,
        "title": entity.title or "",
        "stage_id": stage_id,
        "stage_name": stage_name,
        "category_id": entity.category_id,
        "created_time": entity.created_time.isoformat() if entity.created_time else None,
        "bitrix_url": bitrix_deal_url(portal_id, int(entity.entity_id)),
    }
    if contacts is not None:
        deal["contacts"] = contacts
        deal["company"] = company
        deal["lpr_confidence"] = _deal_lpr_confidence(contacts)
    return deal


def _enrich_deal(deal: dict[str, Any], portal_id: str) -> dict[str, Any]:
    enriched = dict(deal)
    if "bitrix_url" not in enriched or enriched["bitrix_url"] is None:
        enriched["bitrix_url"] = bitrix_deal_url(portal_id, int(deal["deal_id"]))
    return enriched


def _deal_only_dataset(dataset: Dataset) -> Dataset:
    deal_sources = [s for s in dataset.sources if s.entity_type_id == ENTITY_DEAL]
    if not deal_sources:
        raise ValueError("dataset has no deal source")
    primary = deal_sources[0]
    return Dataset(
        id=dataset.id,
        primary_entity_type_id=ENTITY_DEAL,
        sources=[primary],
        relation_refs=[],
        filters=dataset.filters,
        sort=[s for s in dataset.sort if s.field.entity_type_id == ENTITY_DEAL],
        limit=dataset.limit,
        include_deleted=dataset.include_deleted,
    )


def _parse_date(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if len(text) == 10:
                d = date.fromisoformat(text)
                return datetime(d.year, d.month, d.day)
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class ExportDealsService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.portal_id = resolve_portal_id(settings)

    def _exclude_archived_entities(
        self,
        entities: list[CrmEntity],
        *,
        category_id: int | None,
    ) -> list[CrmEntity]:
        if not entities or category_id is None:
            return entities
        client = None
        if self.settings.bitrix_webhook_url:
            client = BitrixClient(self.settings)
        archive_stage_ids = resolve_archive_stage_ids(
            self.db,
            self.portal_id,
            int(category_id),
            client=client,
        )
        return filter_non_archived_deals(entities, archive_stage_ids=archive_stage_ids)

    def _export_archive_stage_ids(self, category_id: int | None) -> list[str]:
        if category_id is None:
            return []
        client = None
        if self.settings.bitrix_webhook_url:
            client = BitrixClient(self.settings)
        archive_stage_ids = resolve_archive_stage_ids(
            self.db,
            self.portal_id,
            int(category_id),
            client=client,
        )
        return list(archive_stage_ids)

    def list_deals(
        self,
        job: ExportJob,
        *,
        source: Literal["filter", "file"],
        offset: int = 0,
        limit: int = 50,
    ) -> ExportDealsResult:
        limit = max(1, min(limit, 200))
        offset = max(0, offset)

        if source == "file":
            return self._list_from_file(job, offset=offset, limit=limit)
        return self._list_from_filter(job, offset=offset, limit=limit)

    def _list_from_file(self, job: ExportJob, *, offset: int, limit: int) -> ExportDealsResult:
        if job.status != "completed" or not job.result_file:
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="file",
                offset=offset,
                limit=limit,
                note="Список из файла доступен после завершения выгрузки",
            )

        path = Path(job.result_file)
        all_deals, available, note = extract_deals_from_result(path, job.mode)
        page = [_enrich_deal(d, self.portal_id) for d in all_deals[offset : offset + limit]]
        return ExportDealsResult(
            total=len(all_deals),
            deals=page,
            available=available,
            source="file",
            offset=offset,
            limit=limit,
            note=note,
        )

    def _list_from_filter(self, job: ExportJob, *, offset: int, limit: int) -> ExportDealsResult:
        params = json.loads(job.parameters_json or "{}")
        if job.mode == "intelligent_export":
            return self._filter_intelligent_export(params, offset=offset, limit=limit)
        if job.mode in ("region", "stage", "region_lpr"):
            return self._filter_legacy_crm(job.mode, params, offset=offset, limit=limit)
        if job.mode == "category_full":
            return self._filter_category_full(params, offset=offset, limit=limit)
        return ExportDealsResult(
            total=0,
            deals=[],
            available=False,
            source="filter",
            offset=offset,
            limit=limit,
            note=f"Режим {job.mode} не поддерживает просмотр сделок по фильтрам",
        )

    def _filter_intelligent_export(
        self, params: dict[str, Any], *, offset: int, limit: int
    ) -> ExportDealsResult:
        plan_version_id = params.get("plan_version_id")
        user_id = params.get("user_id")
        if not plan_version_id or not user_id:
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="filter",
                offset=offset,
                limit=limit,
                note="Не удалось определить план выгрузки",
            )

        version = self.db.get(IeExportPlanVersion, int(plan_version_id))
        user = self.db.get(AppUser, int(user_id))
        if version is None or user is None:
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="filter",
                offset=offset,
                limit=limit,
                note="План или пользователь не найдены",
            )

        scope = build_scope(user, self.settings)
        prepared = prepare_plan(self.db, self.portal_id, scope, version.plan_json)
        if not prepared.valid or prepared.plan is None:
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="filter",
                offset=offset,
                limit=limit,
                note="План выгрузки не прошёл проверку",
            )

        deal_datasets = [d for d in prepared.plan.datasets if d.primary_entity_type_id == ENTITY_DEAL]
        if not deal_datasets:
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="filter",
                offset=offset,
                limit=limit,
                note="В плане нет выборки по сделкам",
            )

        dataset = deal_datasets[0]
        try:
            deal_dataset = _deal_only_dataset(dataset)
        except ValueError:
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="filter",
                offset=offset,
                limit=limit,
                note="Не удалось построить выборку сделок",
            )

        compiler = ExportPlanCompilerV2(
            self.db, self.portal_id, prepared.catalog, scope
        )
        compiled = compiler.compile_dataset(deal_dataset)
        cap = min(deal_dataset.limit, scope.max_rows)
        total = min(compiler.count(compiled, timeout_ms=self.settings.ie_statement_timeout_ms), cap)
        rows = compiler.fetch_page(
            compiled,
            offset=offset,
            limit=limit,
            timeout_ms=self.settings.ie_statement_timeout_ms,
        )
        deals: list[dict[str, Any]] = []
        stage_names = build_stage_names_map(self.db, self.portal_id)
        for row in rows:
            entity = row.get(compiled.primary_alias)
            deal = _entity_to_deal(entity, self.portal_id, stage_names=stage_names)
            if deal is not None:
                deals.append(deal)

        return ExportDealsResult(
            total=total,
            deals=deals,
            available=True,
            source="filter",
            offset=offset,
            limit=limit,
            note=FILTER_NOTE,
        )

    def list_tomoru_deals(
        self,
        *,
        entity_type: str = "deal",
        category_id: int = 15,
        stage_id: str | None = None,
        stage_ids: list[str] | None = None,
        region_id: int | None = None,
        region_ids: list[int] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        offset: int = 0,
        limit: int = 50,
        lpr_view: str = "all",
        confidence_threshold: float = 70.0,
    ) -> ExportDealsResult:
        params: dict[str, Any] = {
            "entity_type": entity_type,
            "category_id": category_id,
        }
        if stage_ids:
            params["stage_ids"] = stage_ids
        elif stage_id:
            params["stage_id"] = stage_id
        if region_ids:
            params["region_ids"] = region_ids
        elif region_id is not None:
            params["region_id"] = region_id
        if date_from is not None:
            params["date_from"] = date_from.isoformat()
        if date_to is not None:
            params["date_to"] = date_to.isoformat()
        return self._filter_legacy_crm(
            "region_lpr",
            params,
            offset=offset,
            limit=limit,
            include_contacts=True,
            lpr_view=lpr_view,
            confidence_threshold=confidence_threshold,
        )

    def _filter_legacy_crm(
        self,
        mode: str,
        params: dict[str, Any],
        *,
        offset: int,
        limit: int,
        include_contacts: bool = False,
        lpr_view: str = "all",
        confidence_threshold: float = 70.0,
    ) -> ExportDealsResult:
        entity_type = params.get("entity_type", "deal")
        if entity_type == "lead":
            return ExportDealsResult(
                total=0,
                deals=[],
                available=False,
                source="filter",
                offset=offset,
                limit=limit,
                note="Для выгрузки лидов список сделок недоступен",
            )

        repo = CrmRepository(self.db, self.portal_id)
        if mode == "region_lpr":
            export_limit: int | None = None
        else:
            export_limit = int(params.get("limit") or self.settings.max_export_size)
        category_id = params.get("category_id")
        region_field = params.get("region_field", "UF_CRM_5ECE25C5D78E0")
        date_from, date_to = _date_range_bounds(
            params.get("date_from"), params.get("date_to")
        )
        exclude_stage_ids = self._export_archive_stage_ids(category_id)
        matched_total = repo.count_entities_for_export(
            ENTITY_DEAL,
            category_id=category_id,
            stage_id=params.get("stage_id"),
            stage_ids=params.get("stage_ids"),
            region_id=params.get("region_id"),
            region_ids=params.get("region_ids"),
            region_field=region_field,
            date_from=date_from,
            date_to=date_to,
            exclude_stage_ids=exclude_stage_ids or None,
        )
        truncated = export_limit is not None and matched_total > export_limit
        entities = repo.list_entities_for_export(
            ENTITY_DEAL,
            category_id=category_id,
            stage_id=params.get("stage_id"),
            stage_ids=params.get("stage_ids"),
            region_id=params.get("region_id"),
            region_ids=params.get("region_ids"),
            region_field=region_field,
            date_from=date_from,
            date_to=date_to,
            limit=export_limit,
            exclude_stage_ids=exclude_stage_ids or None,
        )
        entities = self._exclude_archived_entities(
            entities,
            category_id=category_id,
        )
        lpr_config = load_lpr_config(self.db) if include_contacts else None
        saved_overrides = (
            load_tomoru_contact_overrides(self.db, self.portal_id) if include_contacts else {}
        )
        lpr_pick_cache = load_all(self.db, self.portal_id) if include_contacts else {}
        cache_dirty = [False]
        stage_names = build_stage_names_map(
            self.db,
            self.portal_id,
            category_id=category_id,
        )
        build_entities = entities if include_contacts else entities[offset : offset + limit]
        all_deals: list[dict[str, Any]] = []
        for entity in build_entities:
            deal_id = int(entity.entity_id)
            saved_selection = (
                saved_overrides[deal_id] if deal_id in saved_overrides else None
            )
            contacts = (
                _contacts_for_deal(
                    self.db,
                    self.portal_id,
                    entity,
                    settings=self.settings,
                    lpr_config=lpr_config,
                    saved_selection=saved_selection,
                    lpr_pick_cache=lpr_pick_cache,
                    cache_dirty=cache_dirty,
                )
                if include_contacts
                else None
            )
            company = (
                _company_for_deal(
                    self.db,
                    self.portal_id,
                    entity,
                    selected_for_export=(
                        saved_selection is not None and 0 in saved_selection
                    ),
                )
                if include_contacts
                else None
            )
            deal = _entity_to_deal(
                entity,
                self.portal_id,
                contacts=contacts,
                company=company,
                stage_names=stage_names,
            )
            if deal is not None:
                all_deals.append(deal)
        if include_contacts and cache_dirty[0]:
            save_all(self.db, self.portal_id, lpr_pick_cache)
        note = FILTER_NOTE
        if include_contacts:
            note = f"{FILTER_NOTE} {TOMORU_CONTACTS_NOTE}"
        if truncated:
            note = f"{TRUNCATION_NOTE_TEMPLATE.format(matched_total=matched_total, export_limit=export_limit)} {note}"

        lpr_summary: dict[str, Any] | None = None
        page: list[dict[str, Any]]
        result_offset = offset
        result_limit = limit
        result_total = len(entities)

        if include_contacts:
            threshold = max(0.0, min(100.0, confidence_threshold))
            lpr_summary = _compute_lpr_summary(all_deals, threshold)
            if lpr_view == "uncertain":
                page = [
                    d
                    for d in all_deals
                    if _is_uncertain_deal(d.get("lpr_confidence"), threshold)
                ]
                result_total = len(page)
                result_offset = 0
                result_limit = max(result_total, 1)
            else:
                page = all_deals[offset : offset + limit]
        else:
            page = all_deals

        return ExportDealsResult(
            total=result_total,
            deals=page,
            available=True,
            source="filter",
            offset=result_offset,
            limit=result_limit,
            note=note,
            matched_total=matched_total,
            truncated=truncated,
            lpr_summary=lpr_summary,
            lpr_view=lpr_view if include_contacts else "all",
        )

    def _filter_category_full(
        self, params: dict[str, Any], *, offset: int, limit: int
    ) -> ExportDealsResult:
        repo = CrmRepository(self.db, self.portal_id)
        export_limit = int(params.get("limit") or self.settings.max_export_size)
        entities = repo.list_entities_for_export(
            ENTITY_DEAL,
            category_id=params.get("category_id"),
            limit=export_limit,
        )
        entities = self._exclude_archived_entities(
            entities,
            category_id=params.get("category_id"),
        )
        category_id = params.get("category_id")
        stage_names = build_stage_names_map(
            self.db,
            self.portal_id,
            category_id=category_id,
        )
        all_deals = [
            d
            for e in entities
            if (d := _entity_to_deal(e, self.portal_id, stage_names=stage_names)) is not None
        ]
        page = all_deals[offset : offset + limit]
        return ExportDealsResult(
            total=len(all_deals),
            deals=page,
            available=True,
            source="filter",
            offset=offset,
            limit=limit,
            note=FILTER_NOTE,
        )
