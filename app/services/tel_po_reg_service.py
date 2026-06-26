"""Выгрузка телефонов по региону (логика tel_po_reg)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings, get_export_dir
from app.exceptions import ExportCancelledError, ExportValidationError
from app.services.bitrix_client import BitrixClient
from app.services.excel_service import DealContactsRow, ExcelService, make_export_date
from app.services.json_export_service import build_export_payload, write_export_json
from app.services.export_service import ExportStatistics
from app.services.security_service import safe_filename, unique_filepath

logger = logging.getLogger(__name__)

DEAL_SELECT = ["ID", "TITLE", "UF_CRM_5ECE25C5D78E0", "CATEGORY_ID"]


@dataclass
class ContactPhone:
    fio: str
    phone: str


class TelPoRegService:
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

    def run_region_phones_export(self, params: dict[str, Any]) -> str:
        region_id = params.get("region_id")
        region_name = params.get("region_name", "")
        if not region_id:
            raise ExportValidationError("Не указан ID региона")
        if params.get("limit", 0) > self.settings.max_export_size:
            raise ExportValidationError(
                f"Лимит не может превышать {self.settings.max_export_size}"
            )

        region_field = params.get("region_field", "UF_CRM_5ECE25C5D78E0")
        category_id = params.get("category_id", 15)
        deal_filter = {
            "CATEGORY_ID": category_id,
            region_field: region_id,
        }

        self._log(f"Загрузка сделок для региона «{region_name}» (ID={region_id})")
        deals = self.client.get_deals(deal_filter, DEAL_SELECT, params["limit"])
        if not deals:
            raise ExportValidationError("В указанном регионе отсутствуют сделки")

        self.stats.deals_total = len(deals)
        rows: list[DealContactsRow] = []

        for idx, deal in enumerate(deals, 1):
            self._check_cancel()
            deal_id = int(deal["ID"])
            deal_title = deal.get("TITLE", "")
            self.stats.deals_processed = idx
            self._progress(idx, len(deals), f"Обработка сделки {deal_id}")

            try:
                contacts = self._collect_deal_contacts(deal_id)
                rows.append(
                    DealContactsRow(
                        deal_id=deal_id,
                        deal_title=deal_title,
                        contacts=contacts,
                    )
                )
                self.stats.phones_found += len(contacts)
                self._log(
                    f"Сделка {deal_id}: уникальных телефонов {len(contacts)}"
                )
            except ExportCancelledError:
                raise
            except Exception as exc:
                self.stats.errors += 1
                self._log(f"Ошибка сделки {deal_id}: {exc}")
                logger.exception("Deal processing error %s", deal_id)

        self._check_cancel()
        self._progress(len(deals), len(deals), "Формирование Excel")

        filename = safe_filename("region", region_name or "export")
        export_dir = get_export_dir(self.settings)
        filepath = unique_filepath(export_dir, filename)
        self.excel.build_deals_contacts(rows, filepath)
        export_date = make_export_date()
        info = {
            "Режим": "region",
            "Регион": region_name,
            "ID региона": region_id,
            "Дата выгрузки": export_date,
            "Сделок": self.stats.deals_total,
            "Обработано сделок": self.stats.deals_processed,
            "Контактов": self.stats.contacts_found,
            "Телефонов": self.stats.phones_found,
            "Ошибок": self.stats.errors,
            "Пропущено": self.stats.skipped,
        }
        write_export_json(
            filepath,
            build_export_payload(
                mode="region",
                export_date=export_date,
                info=info,
                data={
                    "deals": [
                        {
                            "deal_id": row.deal_id,
                            "deal_title": row.deal_title,
                            "contacts": row.contacts,
                        }
                        for row in rows
                    ]
                },
            ),
        )
        self._log(f"Файл сохранён: {filepath.name}")
        return str(filepath)

    def _collect_deal_contacts(self, deal_id: int) -> list[ContactPhone]:
        contact_ids, company_phone = self._get_all_deal_contact_ids(deal_id)
        self.stats.contacts_found += len(contact_ids)

        contacts_info: list[ContactPhone] = []
        seen_phones: set[str] = set()

        if company_phone:
            seen_phones.add(company_phone)
            contacts_info.append(ContactPhone(fio="Телефон компании", phone=company_phone))

        for contact_id in contact_ids:
            contact_data = self._get_contact_info(contact_id)
            if contact_data and contact_data.phone:
                if contact_data.phone not in seen_phones:
                    seen_phones.add(contact_data.phone)
                    contacts_info.append(contact_data)

        return contacts_info

    def _get_all_deal_contact_ids(self, deal_id: int) -> tuple[list[int], str | None]:
        all_contact_ids: set[int] = set()
        company_phone: str | None = None

        data = self.client.call("crm.deal.get", {"id": deal_id})
        deal = data.get("result") or {}
        main_contact = deal.get("CONTACT_ID")
        if main_contact and str(main_contact) not in ("None", "0", ""):
            all_contact_ids.add(int(main_contact))

        company_id = deal.get("COMPANY_ID")
        if company_id and str(company_id) not in ("None", "0", ""):
            company_id = int(company_id)
        else:
            company_id = None

        for cid in self.client.get_deal_contacts(deal_id):
            all_contact_ids.add(cid)

        if company_id:
            company = self.client.get_company(company_id)
            if company:
                phones = company.get("PHONE") or []
                if phones and isinstance(phones[0], dict):
                    company_phone = phones[0].get("VALUE", "") or None
            for cid in self.client.get_company_contacts(company_id):
                all_contact_ids.add(cid)

        return list(all_contact_ids), company_phone

    def _get_contact_info(self, contact_id: int) -> ContactPhone | None:
        contact = self.client.get_contact(contact_id)
        if not contact:
            return None
        fio = self.client.format_contact_name(contact)
        phones = contact.get("PHONE") or []
        phone = phones[0].get("VALUE", "") if phones and isinstance(phones[0], dict) else ""
        if not phone:
            return None
        return ContactPhone(fio=fio, phone=phone)
