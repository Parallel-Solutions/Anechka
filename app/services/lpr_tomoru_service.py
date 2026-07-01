"""Выгрузка телефонов ЛПР для обзвона в Tomoru.

Формирует CSV с колонкой phone_number (7XXXXXXXXXX) из локальной БД импортированных CRM-сущностей.
Для AI-инструмента с фильтром по региону сохранён путь через live Bitrix REST API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings, get_export_dir
from app.exceptions import ExportCancelledError, ExportValidationError
from app.models import ENTITY_DEAL, ENTITY_LEAD, CrmContact, CrmEntity
from app.repositories.contact_repository import ContactRepository
from app.repositories.crm_repository import CrmRepository
from app.services.bitrix_client import BitrixClient
from app.services.excel_service import ExcelService, make_export_date
from app.services.export_plan.payload_keys import payload_lookup
from app.services.export_service import ExportStatistics
from app.services.intelligent_export.contact_lpr_classifier import build_lpr_classifier
from app.services.intelligent_export.contact_phone_heuristic import (
    _deal_company_id,
    collect_deal_contacts,
    filter_non_archived_deals,
    pick_company_phone,
    pick_phone_for_contact,
    pick_phone_for_deal,
)
from app.services.export_plan.models_v2 import SheetPostProcess
from app.services.intelligent_export.tomoru_stages import resolve_archive_stage_ids
from app.services.json_export_service import build_export_payload, write_export_json
from app.services.lpr_service import LprConfig, contact_to_lpr_dict, detect_lpr
from app.services.tomoru_contact_preferences import (
    load_all as load_tomoru_contact_overrides,
    merge_with_request,
)
from app.services.phone_service import extract_phones_from_multifield, normalize_phone
from app.services.security_service import safe_filename, unique_filepath

logger = logging.getLogger(__name__)

DEAL_SELECT = [
    "ID",
    "TITLE",
    "UF_CRM_5ECE25C5D78E0",
    "CATEGORY_ID",
    "STAGE_ID",
    "COMPANY_ID",
    "CONTACT_ID",
    "DATE_CREATE",
]


@dataclass
class LprReportRow:
    phone: str
    fio: str
    post: str
    company: str
    deal_id: int
    deal_title: str
    region: str
    reason: str


def _date_range_bounds(
    date_from: date | str | None, date_to: date | str | None
) -> tuple[datetime | None, datetime | None]:
    start: datetime | None = None
    end: datetime | None = None
    if date_from:
        d = date_from if isinstance(date_from, date) else date.fromisoformat(str(date_from))
        start = datetime.combine(d, time.min, tzinfo=timezone.utc)
    if date_to:
        d = date_to if isinstance(date_to, date) else date.fromisoformat(str(date_to))
        end = datetime.combine(d, time.max, tzinfo=timezone.utc)
    return start, end


def _filter_stage_ids(params: dict[str, Any]) -> list[str]:
    stage_ids = list(dict.fromkeys(params.get("stage_ids") or []))
    stage_id = params.get("stage_id")
    if stage_id and stage_id not in stage_ids:
        stage_ids.insert(0, stage_id)
    return [s for s in stage_ids if s]


def _filter_region_ids(params: dict[str, Any]) -> list[int]:
    region_ids = list(dict.fromkeys(params.get("region_ids") or []))
    region_id = params.get("region_id")
    if region_id is not None:
        rid = int(region_id)
        if rid not in region_ids:
            region_ids.insert(0, rid)
    return region_ids


def _filter_region_names(params: dict[str, Any]) -> list[str]:
    region_names = list(params.get("region_names") or [])
    region_name = params.get("region_name")
    if region_name and region_name not in region_names:
        region_names.insert(0, str(region_name))
    return region_names


def _parse_contact_overrides(params: dict[str, Any]) -> dict[int, list[int]]:
    raw = params.get("contact_overrides") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[int, list[int]] = {}
    for key, value in raw.items():
        try:
            deal_id = int(key)
        except (TypeError, ValueError):
            continue
        ids: list[int] = []
        if isinstance(value, list):
            for item in value:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
        else:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        out[deal_id] = list(dict.fromkeys(ids))
    return out


def _tomoru_post_process(category_id: int) -> SheetPostProcess:
    return SheetPostProcess(
        op="tomoru_phones",
        category_id=category_id,
        use_llm_for_lpr=False,
        include_company_contacts=True,
        include_company_phones=True,
        fetch_company_contacts_live=False,
    )


def _contact_lpr_dict(contact: CrmContact) -> dict[str, Any]:
    return contact_to_lpr_dict(contact)


def _entity_lpr_dict(entity: CrmEntity) -> dict[str, Any]:
    raw = dict(entity.raw_payload or {})
    if entity.title:
        raw.setdefault("TITLE", entity.title)
    raw.setdefault("ID", entity.entity_id)
    return raw


def _format_fio_from_dict(data: dict[str, Any]) -> str:
    parts = [
        str(data.get("LAST_NAME") or data.get("lastName") or ""),
        str(data.get("NAME") or data.get("name") or ""),
        str(data.get("SECOND_NAME") or data.get("secondName") or ""),
    ]
    joined = " ".join(p.strip() for p in parts if p and str(p).strip())
    if joined:
        return joined
    return str(data.get("TITLE") or data.get("full_name") or "").strip()


class LprTomoruService:
    def __init__(
        self,
        settings: Settings,
        cancel_check: Callable[[], bool],
        lpr_config: LprConfig,
        db: Session | None = None,
        portal_id: str | None = None,
        progress_callback: Callable[[int, int, str, ExportStatistics], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.cancel_check = cancel_check
        self.lpr_config = lpr_config
        self.db = db
        self.portal_id = portal_id
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.client = BitrixClient(settings, cancel_check=cancel_check) if settings.bitrix_webhook_url else None
        self.excel = ExcelService()
        self.stats = ExportStatistics()
        self.report_rows: list[LprReportRow] = []
        self.phones: list[str] = []
        self.last_matched_total: int = 0
        self.last_truncated: bool = False

    def _reset_export_stats(self) -> None:
        self.last_matched_total = 0
        self.last_truncated = False

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.log_callback:
            self.log_callback(message)

    def _progress(self, current: int, total: int, step: str) -> None:
        if self.progress_callback:
            self.progress_callback(current, total, step, self.stats)

    def _check_cancel(self) -> None:
        if self.cancel_check():
            raise ExportCancelledError()

    def run_lpr_tomoru_export(self, params: dict[str, Any]) -> str:
        region_id = params.get("region_id")
        entity_type = params.get("entity_type")
        self._reset_export_stats()
        if region_id and not entity_type:
            return self._run_bitrix_region_export(params)
        entity_type = str(entity_type or "deal").lower()
        if entity_type not in ("deal", "lead"):
            raise ExportValidationError("entity_type должен быть 'deal' или 'lead'")
        if not self.db or not self.portal_id:
            raise ExportValidationError("Локальная БД недоступна для выгрузки")

        if entity_type == "lead":
            return self._run_db_leads(params)
        return self._run_db_deals(params)

    def _run_db_deals(self, params: dict[str, Any]) -> str:
        category_id = int(params.get("category_id") or 15)
        stage_ids = _filter_stage_ids(params)
        region_ids = _filter_region_ids(params)
        region_names = _filter_region_names(params)
        date_from, date_to = _date_range_bounds(params.get("date_from"), params.get("date_to"))

        crm_repo = CrmRepository(self.db, self.portal_id)
        description_parts = [f"воронка {category_id}", "из локальной БД"]
        if stage_ids:
            description_parts.append("стадии " + ", ".join(stage_ids))
        if region_ids:
            region_labels = []
            for idx, rid in enumerate(region_ids):
                name = region_names[idx] if idx < len(region_names) else ""
                if name:
                    region_labels.append(f"«{name}» (ID={rid})")
                else:
                    region_labels.append(f"ID={rid}")
            description_parts.append("регионы " + ", ".join(region_labels))
        if params.get("date_from") or params.get("date_to"):
            description_parts.append(
                f"дата {params.get('date_from') or '…'} — {params.get('date_to') or '…'}"
            )
        self._log("Загрузка сделок: " + ", ".join(description_parts))

        bitrix_client = None
        if self.settings.bitrix_webhook_url:
            bitrix_client = BitrixClient(self.settings)
        archive_stage_ids = resolve_archive_stage_ids(
            self.db,
            self.portal_id,
            category_id,
            client=bitrix_client,
        )
        exclude_stage_ids = list(archive_stage_ids) if archive_stage_ids else None
        matched_total = crm_repo.count_entities_for_export(
            ENTITY_DEAL,
            category_id=category_id,
            stage_ids=stage_ids or None,
            region_ids=region_ids or None,
            date_from=date_from,
            date_to=date_to,
            exclude_stage_ids=exclude_stage_ids,
        )
        self.last_matched_total = matched_total
        self.last_truncated = False

        deals = crm_repo.list_entities_for_export(
            ENTITY_DEAL,
            category_id=category_id,
            stage_ids=stage_ids or None,
            region_ids=region_ids or None,
            date_from=date_from,
            date_to=date_to,
            exclude_stage_ids=exclude_stage_ids,
        )
        if not deals:
            raise ExportValidationError("По указанным фильтрам сделки не найдены в локальной БД")
        before_archive_filter = len(deals)
        deals = filter_non_archived_deals(deals, archive_stage_ids=archive_stage_ids)
        skipped_archived = before_archive_filter - len(deals)
        if skipped_archived:
            self._log(f"Пропущено архивных сделок: {skipped_archived}")
        if not deals:
            raise ExportValidationError("По указанным фильтрам сделки не найдены в локальной БД")

        self.stats.deals_total = len(deals)
        seen_phones: set[str] = set()
        report_rows: list[LprReportRow] = []
        contact_repo = ContactRepository(self.db, self.portal_id)
        saved_overrides = load_tomoru_contact_overrides(self.db, self.portal_id)
        request_overrides = _parse_contact_overrides(params)
        contact_overrides = merge_with_request(saved_overrides, request_overrides)
        post_process = _tomoru_post_process(category_id)
        classifier = build_lpr_classifier(self.settings, self.lpr_config, use_llm=False)

        def append_phone(
            phone: str,
            *,
            deal_id: int,
            deal_title: str,
            company_name: str,
            reason: str,
            contact: CrmContact | None = None,
        ) -> None:
            if not phone or phone in seen_phones:
                return
            seen_phones.add(phone)
            fio = ""
            post = ""
            if contact is not None:
                fio = contact.full_name or _format_fio_from_dict(_contact_lpr_dict(contact))
                post = str(contact.post or contact.post_custom or "")
            report_rows.append(
                LprReportRow(
                    phone=phone,
                    fio=fio,
                    post=post,
                    company=company_name,
                    deal_id=deal_id,
                    deal_title=deal_title,
                    region="",
                    reason=reason,
                )
            )
            self.stats.phones_found += 1

        for idx, deal in enumerate(deals, 1):
            self._check_cancel()
            deal_id = int(deal.entity_id)
            deal_title = deal.title or ""
            self.stats.deals_processed = idx
            self._progress(idx, len(deals), f"Обработка сделки {deal_id}")

            try:
                company_name = self._company_title_from_deal(deal)
                candidates = collect_deal_contacts(
                    self.db,
                    self.portal_id,
                    deal,
                    include_company_contacts=True,
                )
                self.stats.contacts_found += len(candidates)

                if deal_id in contact_overrides:
                    override_ids = contact_overrides[deal_id]
                    for override_contact_id in override_ids:
                        phone: str | None = None
                        reason = "ручной выбор"
                        contact: CrmContact | None = None
                        if override_contact_id > 0:
                            contact = contact_repo.get_contact(override_contact_id)
                            phone = pick_phone_for_contact(
                                self.db, self.portal_id, override_contact_id
                            )
                        elif post_process.include_company_phones:
                            company_id = _deal_company_id(deal)
                            if company_id:
                                phone = pick_company_phone(
                                    self.db,
                                    self.portal_id,
                                    company_id,
                                    bitrix_client=bitrix_client,
                                )
                                reason = "телефон компании"
                        append_phone(
                            phone or "",
                            deal_id=deal_id,
                            deal_title=deal_title,
                            company_name=company_name,
                            reason=reason,
                            contact=contact,
                        )
                else:
                    pick = pick_phone_for_deal(
                        self.db,
                        self.portal_id,
                        deal,
                        post_process=post_process,
                        lpr_config=self.lpr_config,
                        classifier=classifier,
                        bitrix_client=bitrix_client,
                    )
                    picked_contact: CrmContact | None = None
                    if pick.contact_id:
                        picked_contact = contact_repo.get_contact(pick.contact_id)
                    append_phone(
                        pick.phone or "",
                        deal_id=deal_id,
                        deal_title=deal_title,
                        company_name=company_name,
                        reason=pick.reason or "эвристика",
                        contact=picked_contact,
                    )
            except ExportCancelledError:
                raise
            except Exception as exc:
                self.stats.errors += 1
                self._log(f"Ошибка сделки {deal_id}: {exc}")
                logger.exception("LPR deal processing error %s", deal_id)

        self._check_cancel()
        self._progress(len(deals), len(deals), "Формирование CSV")

        return self._write_export(
            report_rows=report_rows,
            base_label=f"category_{category_id}",
            info_extra={
                "Сущность": "Сделки",
                "Источник": "локальная БД",
                "Воронка": category_id,
                "Стадии": ", ".join(stage_ids),
                "Регионы": ", ".join(
                    region_names[idx] if idx < len(region_names) else str(rid)
                    for idx, rid in enumerate(region_ids)
                ),
                "Дата создания с": str(params.get("date_from") or ""),
                "Дата создания по": str(params.get("date_to") or ""),
            },
        )

    def _run_db_leads(self, params: dict[str, Any]) -> str:
        stage_ids = _filter_stage_ids(params)
        date_from, date_to = _date_range_bounds(params.get("date_from"), params.get("date_to"))

        crm_repo = CrmRepository(self.db, self.portal_id)
        description_parts = ["лиды", "из локальной БД"]
        if stage_ids:
            description_parts.append("статусы " + ", ".join(stage_ids))
        if params.get("date_from") or params.get("date_to"):
            description_parts.append(
                f"дата {params.get('date_from') or '…'} — {params.get('date_to') or '…'}"
            )
        self._log("Загрузка лидов: " + ", ".join(description_parts))

        matched_total = crm_repo.count_entities_for_export(
            ENTITY_LEAD,
            stage_ids=stage_ids or None,
            date_from=date_from,
            date_to=date_to,
        )
        self.last_matched_total = matched_total
        self.last_truncated = False

        leads = crm_repo.list_entities_for_export(
            ENTITY_LEAD,
            stage_ids=stage_ids or None,
            date_from=date_from,
            date_to=date_to,
        )
        if not leads:
            raise ExportValidationError("По указанным фильтрам лиды не найдены в локальной БД")

        self.stats.deals_total = len(leads)
        seen_phones: set[str] = set()
        report_rows: list[LprReportRow] = []
        contact_repo = ContactRepository(self.db, self.portal_id)

        for idx, lead in enumerate(leads, 1):
            self._check_cancel()
            lead_id = int(lead.entity_id)
            lead_title = lead.title or ""
            self.stats.deals_processed = idx
            self._progress(idx, len(leads), f"Обработка лида {lead_id}")

            try:
                company_name = str(
                    payload_lookup(lead.raw_payload or {}, "COMPANY_TITLE")
                    or payload_lookup(lead.raw_payload or {}, "companyTitle")
                    or ""
                )
                self._collect_entity_lpr_phones(
                    entity=lead,
                    entity_id=lead_id,
                    entity_title=lead_title,
                    company_name=company_name,
                    contact_repo=contact_repo,
                    seen_phones=seen_phones,
                    report_rows=report_rows,
                    parent_entity_type=ENTITY_LEAD,
                )
            except ExportCancelledError:
                raise
            except Exception as exc:
                self.stats.errors += 1
                self._log(f"Ошибка лида {lead_id}: {exc}")
                logger.exception("LPR lead processing error %s", lead_id)

        self._check_cancel()
        self._progress(len(leads), len(leads), "Формирование CSV")

        return self._write_export(
            report_rows=report_rows,
            base_label=f"leads_{stage_ids[0]}" if len(stage_ids) == 1 else "leads",
            info_extra={
                "Сущность": "Лиды",
                "Источник": "локальная БД",
                "Статусы": ", ".join(stage_ids),
                "Дата создания с": str(params.get("date_from") or ""),
                "Дата создания по": str(params.get("date_to") or ""),
            },
        )

    def _collect_entity_lpr_phones(
        self,
        *,
        entity: CrmEntity,
        entity_id: int,
        entity_title: str,
        company_name: str,
        contact_repo: ContactRepository,
        seen_phones: set[str],
        report_rows: list[LprReportRow],
        parent_entity_type: int,
    ) -> None:
        entity_dict = _entity_lpr_dict(entity)
        own_is_lpr, own_reason = detect_lpr(entity_dict, self.lpr_config)
        if own_is_lpr:
            fio = _format_fio_from_dict(entity_dict)
            post = str(entity_dict.get("POST") or "")
            for raw, _ptype in extract_phones_from_multifield(entity_dict.get("PHONE")):
                normalized = normalize_phone(raw)
                if not normalized or normalized in seen_phones:
                    continue
                seen_phones.add(normalized)
                report_rows.append(
                    LprReportRow(
                        phone=normalized,
                        fio=fio,
                        post=post,
                        company=company_name,
                        deal_id=entity_id,
                        deal_title=entity_title,
                        region="",
                        reason=own_reason,
                    )
                )
                self.stats.phones_found += 1

        for row in contact_repo.get_contacts_for_parent(parent_entity_type, entity_id):
            contact = row.get("contact")
            if contact is None:
                continue
            self.stats.contacts_found += 1
            is_lpr, reason = detect_lpr(_contact_lpr_dict(contact), self.lpr_config)
            if not is_lpr:
                continue
            fio = contact.full_name or _format_fio_from_dict(_contact_lpr_dict(contact))
            post = str(contact.post or contact.post_custom or "")
            phones = contact_repo.get_phones_for_contact(int(contact.contact_id))
            for phone_row in phones:
                normalized = normalize_phone(phone_row.get("value") or "")
                if not normalized or normalized in seen_phones:
                    continue
                seen_phones.add(normalized)
                report_rows.append(
                    LprReportRow(
                        phone=normalized,
                        fio=fio,
                        post=post,
                        company=company_name,
                        deal_id=entity_id,
                        deal_title=entity_title,
                        region="",
                        reason=reason,
                    )
                )
                self.stats.phones_found += 1

    def _company_title_from_deal(self, deal: CrmEntity) -> str:
        company_id = payload_lookup(deal.raw_payload or {}, "COMPANY_ID")
        if not company_id:
            company_id = payload_lookup(deal.raw_payload or {}, "companyId")
        try:
            cid = int(company_id)
        except (TypeError, ValueError):
            return ""
        if cid <= 0:
            return ""
        company = CrmRepository(self.db, self.portal_id).get_entity(4, cid)
        if company and company.title:
            return company.title
        if company:
            return str(payload_lookup(company.raw_payload or {}, "TITLE") or "")
        return ""

    def _run_bitrix_region_export(self, params: dict[str, Any]) -> str:
        if not self.client:
            raise ExportValidationError("Bitrix webhook не настроен для региональной выгрузки")

        region_id = params.get("region_id")
        region_name = str(params.get("region_name", "") or "")
        if not region_id:
            raise ExportValidationError("Не указан ID региона")

        region_field = params.get("region_field", "UF_CRM_5ECE25C5D78E0")
        category_id = params.get("category_id", 15)
        deal_filter = {
            "CATEGORY_ID": category_id,
            region_field: region_id,
        }

        self._log(f"Загрузка сделок региона «{region_name}» (ID={region_id}), воронка {category_id}")
        deals = self.client.get_deals(deal_filter, DEAL_SELECT, None)
        if not deals:
            raise ExportValidationError("В указанном регионе отсутствуют сделки")

        self.stats.deals_total = len(deals)
        seen_phones: set[str] = set()
        report_rows: list[LprReportRow] = []

        for idx, deal in enumerate(deals, 1):
            self._check_cancel()
            deal_id = int(deal["ID"])
            deal_title = deal.get("TITLE", "")
            self.stats.deals_processed = idx
            self._progress(idx, len(deals), f"Обработка сделки {deal_id}")

            try:
                contact_ids, company_name = self._collect_deal_contact_ids_bitrix(deal_id)
                self.stats.contacts_found += len(contact_ids)
                for contact_id in contact_ids:
                    self._check_cancel()
                    contact = self.client.get_contact(contact_id)
                    if not contact:
                        continue
                    is_lpr, reason = detect_lpr(contact, self.lpr_config)
                    if not is_lpr:
                        continue
                    fio = self.client.format_contact_name(contact)
                    post = str(contact.get("POST", "") or "")
                    phones = extract_phones_from_multifield(contact.get("PHONE"))
                    for raw, _ptype in phones:
                        normalized = normalize_phone(raw)
                        if not normalized or normalized in seen_phones:
                            continue
                        seen_phones.add(normalized)
                        report_rows.append(
                            LprReportRow(
                                phone=normalized,
                                fio=fio,
                                post=post,
                                company=company_name,
                                deal_id=deal_id,
                                deal_title=deal_title,
                                region=region_name,
                                reason=reason,
                            )
                        )
                        self.stats.phones_found += 1
            except ExportCancelledError:
                raise
            except Exception as exc:
                self.stats.errors += 1
                self._log(f"Ошибка сделки {deal_id}: {exc}")
                logger.exception("LPR deal processing error %s", deal_id)

        self._check_cancel()
        self._progress(len(deals), len(deals), "Формирование CSV")

        return self._write_export(
            report_rows=report_rows,
            base_label=region_name or "export",
            info_extra={
                "Источник": "Bitrix REST API",
                "Регион": region_name,
                "ID региона": region_id,
                "Воронка": category_id,
            },
        )

    def _write_export(
        self,
        *,
        report_rows: list[LprReportRow],
        base_label: str,
        info_extra: dict[str, Any],
    ) -> str:
        self.report_rows = report_rows
        self.phones = [r.phone for r in report_rows]

        if not report_rows:
            self._log("ЛПР с телефонами не найдены — формируется пустой файл")

        filename = safe_filename("lpr_tomoru", base_label or "export", ext="csv")
        export_dir = get_export_dir(self.settings)
        filepath = unique_filepath(export_dir, filename)
        self.excel.write_tomoru_numbers_csv(self.phones, filepath)
        export_date = make_export_date()
        info: dict[str, Any] = {
            "Режим": "region_lpr",
            "Дата выгрузки": export_date,
            "Сделок": self.stats.deals_total,
            "Обработано": self.stats.deals_processed,
            "Контактов": self.stats.contacts_found,
            "Телефонов": self.stats.phones_found,
            "Ошибок": self.stats.errors,
            "Пропущено": self.stats.skipped,
        }
        info.update(info_extra)
        write_export_json(
            filepath,
            build_export_payload(
                mode="region_lpr",
                export_date=export_date,
                info=info,
                data={"phones": self.phones, "report": report_rows},
            ),
        )
        self._log(f"Файл сохранён: {filepath.name}. ЛПР-телефонов: {len(self.phones)}")
        return str(filepath)

    def _collect_deal_contact_ids_bitrix(self, deal_id: int) -> tuple[list[int], str]:
        contact_ids: set[int] = set()
        company_name = ""

        data = self.client.call("crm.deal.get", {"id": deal_id})
        deal = data.get("result") or {}

        main_contact = deal.get("CONTACT_ID")
        if main_contact and str(main_contact) not in ("None", "0", ""):
            contact_ids.add(int(main_contact))

        company_id = deal.get("COMPANY_ID")
        if company_id and str(company_id) not in ("None", "0", ""):
            company_id = int(company_id)
        else:
            company_id = None

        for cid in self.client.get_deal_contacts(deal_id):
            contact_ids.add(cid)

        if company_id:
            company = self.client.get_company(company_id)
            if company:
                company_name = str(company.get("TITLE", "") or "")
            for cid in self.client.get_company_contacts(company_id):
                contact_ids.add(cid)

        return list(contact_ids), company_name
