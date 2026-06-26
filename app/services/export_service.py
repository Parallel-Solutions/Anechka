"""Сервис выгрузки сделок."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings, get_export_dir
from app.exceptions import ExportCancelledError, ExportValidationError
from app.services.bitrix_client import BitrixClient
from app.services.excel_service import ExcelService, NormalizedRow, WideRow, make_export_date
from app.services.json_export_service import build_export_payload, write_export_json
from app.services.phone_service import (
    PhoneEntry,
    PhoneSource,
    add_phone_entries,
    dedup_phones_for_wide,
    extract_phones_from_multifield,
)
from app.services.security_service import mask_phone, safe_filename, unique_filepath

logger = logging.getLogger(__name__)

DEAL_SELECT = [
    "ID",
    "TITLE",
    "CATEGORY_ID",
    "STAGE_ID",
    "ASSIGNED_BY_ID",
    "COMPANY_ID",
    "CONTACT_ID",
]


@dataclass
class ExportStatistics:
    deals_total: int = 0
    deals_processed: int = 0
    contacts_found: int = 0
    phones_found: int = 0
    errors: int = 0
    skipped: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "deals_total": self.deals_total,
            "deals_processed": self.deals_processed,
            "contacts_found": self.contacts_found,
            "phones_found": self.phones_found,
            "errors": self.errors,
            "skipped": self.skipped,
        }


@dataclass
class ExportContext:
    client: BitrixClient
    settings: Settings
    stats: ExportStatistics = field(default_factory=ExportStatistics)
    region_names: dict[int, str] = field(default_factory=dict)
    category_names: dict[int, str] = field(default_factory=dict)
    stage_names: dict[str, str] = field(default_factory=dict)
    region_label: str = ""
    export_date: str = field(default_factory=make_export_date)


class ExportService:
    def __init__(
        self,
        settings: Settings,
        cancel_check: Callable[[], bool],
        progress_callback: Callable[[int, int, str, ExportStatistics], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.cancel_check = cancel_check
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.client = BitrixClient(settings, cancel_check=cancel_check)
        self.excel = ExcelService()

    def _log(self, message: str) -> None:
        logger.info(message)
        if self.log_callback:
            self.log_callback(message)

    def _progress(self, current: int, total: int, step: str, stats: ExportStatistics) -> None:
        if self.progress_callback:
            self.progress_callback(current, total, step, stats)

    def _check_cancel(self) -> None:
        if self.cancel_check():
            raise ExportCancelledError()

    def run_stage_export(self, params: dict[str, Any]) -> str:
        if params.get("limit", 0) > self.settings.max_export_size:
            raise ExportValidationError(
                f"Лимит не может превышать {self.settings.max_export_size}"
            )

        category_id = params["category_id"]
        stage_id = params["stage_id"]
        stages = self.client.get_stages(category_id)
        if not any(s["id"] == stage_id for s in stages):
            raise ExportValidationError("Стадия не принадлежит выбранной категории")

        ctx = ExportContext(client=self.client, settings=self.settings)
        self._load_metadata(ctx)

        deal_filter: dict[str, Any] = {
            "CATEGORY_ID": category_id,
            "STAGE_ID": stage_id,
        }
        excluded = params.get("excluded_user_ids") or []
        if excluded:
            deal_filter["!ASSIGNED_BY_ID"] = excluded

        self._log(f"Загрузка сделок для стадии {stage_id}")
        deals = self.client.get_deals(
            deal_filter,
            DEAL_SELECT + [params.get("region_field", "UF_CRM_5ECE25C5D78E0")],
            params["limit"],
            region_field=params.get("region_field"),
        )
        if not deals:
            raise ExportValidationError("В выбранной стадии отсутствуют сделки")

        stage_name = next((s["name"] for s in stages if s["id"] == stage_id), stage_id)
        ctx.region_label = stage_name
        return self._process_deals(deals, params, ctx)

    def _load_metadata(self, ctx: ExportContext) -> None:
        for cat in self.client.get_categories():
            ctx.category_names[cat["id"]] = cat["name"]
        for cat_id in ctx.category_names:
            for stage in self.client.get_stages(cat_id):
                ctx.stage_names[stage["id"]] = stage["name"]

    def _process_deals(self, deals: list[dict], params: dict[str, Any], ctx: ExportContext) -> str:
        ctx.stats.deals_total = len(deals)
        normalized_rows: list[NormalizedRow] = []
        wide_rows: list[WideRow] = []
        region_field = params.get("region_field", "UF_CRM_5ECE25C5D78E0")

        for idx, deal in enumerate(deals, 1):
            self._check_cancel()
            deal_id = int(deal["ID"])
            ctx.stats.deals_processed = idx
            self._progress(idx, len(deals), f"Обработка сделки {deal_id}", ctx.stats)

            try:
                row_data = self._process_single_deal(deal, params, ctx, region_field)
                normalized_rows.extend(row_data["normalized"])
                wide_rows.append(row_data["wide"])
            except ExportCancelledError:
                raise
            except Exception as exc:
                ctx.stats.errors += 1
                self._log(f"Ошибка сделки {deal_id}: {exc}")
                logger.exception("Deal processing error %s", deal_id)

        self._check_cancel()
        self._progress(len(deals), len(deals), "Формирование Excel", ctx.stats)

        mode = params.get("_mode", "region")
        label = ctx.region_label or "export"
        filename = safe_filename(mode, label)
        export_dir = get_export_dir(self.settings)
        filepath = unique_filepath(export_dir, filename)

        info = self._build_info(params, ctx)
        excel_format = params.get("excel_format", "normalized")
        if excel_format == "wide":
            self.excel.build_wide(wide_rows, info, filepath)
            json_data = {"format": "wide", "rows": wide_rows}
        else:
            self.excel.build_normalized(normalized_rows, info, filepath)
            json_data = {"format": "normalized", "rows": normalized_rows}

        write_export_json(
            filepath,
            build_export_payload(
                mode=params.get("_mode", "stage"),
                export_date=ctx.export_date,
                info=info,
                data=json_data,
            ),
        )

        self._log(f"Файл сохранён: {filepath.name}")
        return str(filepath)

    def _process_single_deal(
        self,
        deal: dict,
        params: dict[str, Any],
        ctx: ExportContext,
        region_field: str,
    ) -> dict[str, Any]:
        deal_id = int(deal["ID"])
        deal_title = deal.get("TITLE", "")
        category_id = int(deal.get("CATEGORY_ID") or 0)
        stage_id = deal.get("STAGE_ID", "")
        assigned_id = int(deal["ASSIGNED_BY_ID"]) if deal.get("ASSIGNED_BY_ID") else None
        company_id = int(deal["COMPANY_ID"]) if deal.get("COMPANY_ID") not in (None, "", "0", 0) else None
        primary_contact_id = (
            int(deal["CONTACT_ID"]) if deal.get("CONTACT_ID") not in (None, "", "0", 0) else None
        )
        region_val = deal.get(region_field, "")
        region_str = ctx.region_names.get(int(region_val), str(region_val)) if region_val else ""

        assigned_name = ""
        if assigned_id:
            user = self.client.get_user(assigned_id)
            if user:
                assigned_name = self.client.format_user_name(user)

        category_name = ctx.category_names.get(category_id, str(category_id))
        stage_name = ctx.stage_names.get(stage_id, stage_id)

        phone_entries: list[PhoneEntry] = []
        contact_ids: set[int] = set()

        if primary_contact_id:
            contact_ids.add(primary_contact_id)
        try:
            for cid in self.client.get_deal_contacts(deal_id):
                contact_ids.add(cid)
        except Exception as exc:
            ctx.stats.errors += 1
            self._log(f"Ошибка контактов сделки {deal_id}: {exc}")

        if company_id and params.get("include_company_contacts", True):
            try:
                for cid in self.client.get_company_contacts(company_id):
                    contact_ids.add(cid)
            except Exception as exc:
                ctx.stats.errors += 1
                self._log(f"Ошибка контактов компании {company_id}: {exc}")

        company_name = ""
        if company_id:
            try:
                company = self.client.get_company(company_id)
                if company:
                    company_name = company.get("TITLE", "")
                    if params.get("include_company_phones", True):
                        phones = extract_phones_from_multifield(company.get("PHONE"))
                        add_phone_entries(
                            phone_entries,
                            phones,
                            PhoneSource.COMPANY_PHONE,
                            contact_name="Телефон компании",
                            dedup_within_contact=False,
                        )
            except Exception as exc:
                ctx.stats.errors += 1
                self._log(f"Ошибка компании {company_id}: {exc}")

        ctx.stats.contacts_found += len(contact_ids)

        all_phones = params.get("all_contact_phones", True)
        for cid in contact_ids:
            try:
                contact = self.client.get_contact(cid)
                if not contact:
                    ctx.stats.skipped += 1
                    continue
                cname = self.client.format_contact_name(contact)
                source = (
                    PhoneSource.PRIMARY_CONTACT
                    if cid == primary_contact_id
                    else PhoneSource.DEAL_CONTACT
                )
                if company_id and cid not in (primary_contact_id,):
                    comp_contacts = self.client.company_contacts_cache.get(company_id, [])
                    if cid in comp_contacts:
                        source = PhoneSource.COMPANY_CONTACT

                phones = extract_phones_from_multifield(contact.get("PHONE"))
                if not all_phones and phones:
                    phones = phones[:1]
                add_phone_entries(
                    phone_entries,
                    phones,
                    source,
                    contact_id=cid,
                    contact_name=cname,
                )
            except Exception as exc:
                ctx.stats.errors += 1
                self._log(f"Ошибка контакта {cid}: {exc}")

        ctx.stats.phones_found += len(phone_entries)

        normalized: list[NormalizedRow] = []
        for entry in phone_entries:
            normalized.append(
                NormalizedRow(
                    deal_id=deal_id,
                    deal_title=deal_title,
                    category_id=category_id,
                    category_name=category_name,
                    stage_id=stage_id,
                    stage_name=stage_name,
                    assigned_id=assigned_id,
                    assigned_name=assigned_name,
                    company_id=company_id,
                    company_name=company_name,
                    contact_id=entry.contact_id,
                    contact_name=entry.contact_name,
                    raw_phone=entry.raw,
                    normalized_phone=entry.normalized,
                    phone_type=entry.phone_type,
                    phone_source=entry.source.value,
                    region=region_str,
                    export_date=ctx.export_date,
                )
            )

        wide_phones = dedup_phones_for_wide(phone_entries)
        wide_contacts = [
            (p.contact_name or p.source.value, p.raw) for p in wide_phones
        ]
        wide = WideRow(
            employee_name=assigned_name,
            deal_id=deal_id,
            deal_title=deal_title,
            region=region_str,
            contacts=wide_contacts,
        )

        if phone_entries:
            self._log(
                f"Сделка {deal_id}: контактов {len(contact_ids)}, "
                f"телефонов {len(phone_entries)} (последний: {mask_phone(phone_entries[-1].raw)})"
            )

        return {"normalized": normalized, "wide": wide}

    def _build_info(self, params: dict[str, Any], ctx: ExportContext) -> dict[str, Any]:
        safe_params = {k: v for k, v in params.items() if k not in ("bitrix_webhook_url",)}
        return {
            "Режим": params.get("_mode", ""),
            "Параметры": json.dumps(safe_params, ensure_ascii=False),
            "Дата выгрузки": ctx.export_date,
            "Сделок": ctx.stats.deals_total,
            "Обработано сделок": ctx.stats.deals_processed,
            "Контактов": ctx.stats.contacts_found,
            "Телефонов": ctx.stats.phones_found,
            "Ошибок": ctx.stats.errors,
            "Пропущено": ctx.stats.skipped,
        }
