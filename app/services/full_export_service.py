"""Полная выгрузка сделок по категории со всеми полями Bitrix."""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from app.config import Settings, get_export_dir
from app.exceptions import ExportCancelledError, ExportValidationError
from app.services.bitrix_client import BitrixClient
from app.services.excel_service import ExcelService, make_export_date
from app.services.json_export_service import build_export_payload, write_export_json
from app.services.export_service import ExportStatistics
from app.services.security_service import safe_filename, unique_filepath

logger = logging.getLogger(__name__)


class FullCategoryExportService:
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
        self.stats = ExportStatistics()

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

    def run_category_full_export(self, params: dict[str, Any]) -> str:
        limit = params.get("limit", self.settings.max_export_size)
        if limit > self.settings.max_export_size:
            raise ExportValidationError(
                f"Лимит не может превышать {self.settings.max_export_size}"
            )

        category_id = params["category_id"]
        categories = self.client.get_categories()
        category_name = next(
            (c["name"] for c in categories if c["id"] == category_id),
            str(category_id),
        )
        if not any(c["id"] == category_id for c in categories):
            raise ExportValidationError("Категория не найдена")

        self._log("Загрузка схем полей deal / contact / company")
        deal_field_titles = self.client.get_entity_fields("deal")
        contact_field_titles = self.client.get_entity_fields("contact")
        company_field_titles = self.client.get_entity_fields("company")

        excluded = params.get("excluded_user_ids") or []
        self._log(f"Поиск сделок категории «{category_name}» (ID={category_id})")
        deal_ids = self.client.list_deal_ids(category_id, limit, excluded)
        if not deal_ids:
            raise ExportValidationError("В выбранной категории отсутствуют сделки")

        self.stats.deals_total = len(deal_ids)
        self._log(f"Найдено сделок: {len(deal_ids)}")

        stage_names: dict[str, str] = {}
        for stage in self.client.get_stages(category_id):
            stage_names[stage["id"]] = stage["name"]

        deals: list[dict[str, Any]] = []
        total_steps = len(deal_ids) + 3
        self._progress(0, total_steps, "Загрузка сделок")

        deal_map = self.client.batch_get("crm.deal.get", deal_ids)
        for idx, deal_id in enumerate(deal_ids, 1):
            self._check_cancel()
            deal = deal_map.get(deal_id)
            if not deal:
                self.stats.skipped += 1
                continue
            enriched = dict(deal)
            cat_id = int(deal.get("CATEGORY_ID") or category_id)
            enriched["_category_name"] = category_name
            stage_id = deal.get("STAGE_ID", "")
            enriched["_stage_name"] = stage_names.get(str(stage_id), str(stage_id))
            assigned_id = deal.get("ASSIGNED_BY_ID")
            if assigned_id and str(assigned_id) not in ("0", ""):
                user = self.client.get_user(int(assigned_id))
                enriched["_assigned_name"] = (
                    self.client.format_user_name(user) if user else str(assigned_id)
                )
            else:
                enriched["_assigned_name"] = ""
            deals.append(enriched)
            self.stats.deals_processed = idx
            self._progress(idx, total_steps, f"Сделка {deal_id} ({idx}/{len(deal_ids)})")

        self._log("Загрузка связей сделка → контакт")
        deal_contacts_raw = self.client.batch_deal_contacts(deal_ids)
        deal_contacts_rows: list[dict[str, Any]] = []
        contact_ids: set[int] = set()
        company_ids: set[int] = set()

        for deal_id, items in deal_contacts_raw.items():
            for item in items:
                cid = item.get("CONTACT_ID")
                if cid:
                    contact_ids.add(int(cid))
                deal_contacts_rows.append(
                    {
                        "DEAL_ID": deal_id,
                        "CONTACT_ID": cid,
                        "IS_PRIMARY": item.get("IS_PRIMARY", ""),
                        "SORT": item.get("SORT", ""),
                    }
                )

        for deal in deals:
            primary = deal.get("CONTACT_ID")
            if primary and str(primary) not in ("0", "", "None"):
                contact_ids.add(int(primary))
            company_id = deal.get("COMPANY_ID")
            if company_id and str(company_id) not in ("0", "", "None"):
                company_ids.add(int(company_id))

        self._progress(len(deal_ids) + 1, total_steps, "Загрузка контактов")
        self._log(f"Загрузка {len(contact_ids)} контактов")
        contact_map = self.client.batch_get("crm.contact.get", sorted(contact_ids))
        contacts = list(contact_map.values())
        self.stats.contacts_found = len(contacts)

        self._progress(len(deal_ids) + 2, total_steps, "Загрузка компаний")
        self._log(f"Загрузка {len(company_ids)} компаний")
        company_map = self.client.batch_get("crm.company.get", sorted(company_ids))
        companies = list(company_map.values())

        self._progress(total_steps, total_steps, "Формирование Excel")

        filename = safe_filename("category_full", category_name)
        export_dir = get_export_dir(self.settings)
        filepath = unique_filepath(export_dir, filename)

        info = self._build_info(params, category_name, deal_contacts_rows)
        self.excel.build_full_export(
            deals=deals,
            contacts=contacts,
            companies=companies,
            deal_contacts=deal_contacts_rows,
            deal_field_titles=deal_field_titles,
            contact_field_titles=contact_field_titles,
            company_field_titles=company_field_titles,
            info=info,
            filepath=filepath,
        )
        write_export_json(
            filepath,
            build_export_payload(
                mode="category_full",
                export_date=info.get("Дата выгрузки", make_export_date()),
                info=info,
                data={
                    "deals": deals,
                    "contacts": contacts,
                    "companies": companies,
                    "deal_contacts": deal_contacts_rows,
                },
            ),
        )
        self._log(f"Файл сохранён: {filepath.name}")
        return str(filepath)

    def _build_info(
        self,
        params: dict[str, Any],
        category_name: str,
        deal_contacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        safe_params = {k: v for k, v in params.items() if "webhook" not in k.lower()}
        return {
            "Режим": "category_full",
            "Категория": category_name,
            "Параметры": json.dumps(safe_params, ensure_ascii=False),
            "Дата выгрузки": make_export_date(),
            "Сделок": self.stats.deals_total,
            "Обработано сделок": self.stats.deals_processed,
            "Контактов": self.stats.contacts_found,
            "Связей DealContacts": len(deal_contacts),
            "Ошибок": self.stats.errors,
            "Пропущено": self.stats.skipped,
            "Примечание": "При таймаутах увеличьте read_timeout в Настройках",
        }
