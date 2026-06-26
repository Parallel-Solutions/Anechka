"""Выгрузка телефонов ЛПР по региону для обзвона в Tomoru.

Формирует двухлистовой Excel:
- лист «Номера» — телефоны в формате 7XXXXXXXXXX (только цифры), по одному в столбик;
- лист «Отчёт» — человекочитаемые данные, на которых основана выборка.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings, get_export_dir
from app.exceptions import ExportCancelledError, ExportValidationError
from app.services.bitrix_client import BitrixClient
from app.services.excel_service import ExcelService, make_export_date
from app.services.json_export_service import build_export_payload, write_export_json
from app.services.export_service import ExportStatistics
from app.services.lpr_service import LprConfig, detect_lpr
from app.services.phone_service import extract_phones_from_multifield, normalize_phone
from app.services.security_service import safe_filename, unique_filepath

logger = logging.getLogger(__name__)

DEAL_SELECT = ["ID", "TITLE", "UF_CRM_5ECE25C5D78E0", "CATEGORY_ID", "COMPANY_ID", "CONTACT_ID"]


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


class LprTomoruService:
    def __init__(
        self,
        settings: Settings,
        cancel_check: Callable[[], bool],
        lpr_config: LprConfig,
        progress_callback: Callable[[int, int, str, ExportStatistics], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.cancel_check = cancel_check
        self.lpr_config = lpr_config
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.client = BitrixClient(settings, cancel_check=cancel_check)
        self.excel = ExcelService()
        self.stats = ExportStatistics()
        self.report_rows: list[LprReportRow] = []
        self.phones: list[str] = []

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
        region_name = params.get("region_name", "")
        if not region_id:
            raise ExportValidationError("Не указан ID региона")
        limit = int(params.get("limit") or 500)
        if limit > self.settings.max_export_size:
            raise ExportValidationError(
                f"Лимит не может превышать {self.settings.max_export_size}"
            )

        region_field = params.get("region_field", "UF_CRM_5ECE25C5D78E0")
        category_id = params.get("category_id", 15)
        deal_filter = {
            "CATEGORY_ID": category_id,
            region_field: region_id,
        }

        self._log(f"Загрузка сделок региона «{region_name}» (ID={region_id}), воронка {category_id}")
        deals = self.client.get_deals(deal_filter, DEAL_SELECT, limit)
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
                contact_ids, company_name = self._collect_deal_contact_ids(deal_id)
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
                        if not normalized:
                            continue
                        if normalized in seen_phones:
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
        self._progress(len(deals), len(deals), "Формирование Excel")

        self.report_rows = report_rows
        self.phones = [r.phone for r in report_rows]

        if not report_rows:
            self._log("ЛПР с телефонами не найдены — формируется пустой файл")

        filename = safe_filename("lpr_tomoru", region_name or "export")
        export_dir = get_export_dir(self.settings)
        filepath = unique_filepath(export_dir, filename)
        self.excel.build_lpr_tomoru(self.phones, report_rows, filepath)
        export_date = make_export_date()
        info = {
            "Режим": "region_lpr",
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
                mode="region_lpr",
                export_date=export_date,
                info=info,
                data={"phones": self.phones, "report": report_rows},
            ),
        )
        self._log(
            f"Файл сохранён: {filepath.name}. ЛПР-телефонов: {len(self.phones)}"
        )
        return str(filepath)

    def _collect_deal_contact_ids(self, deal_id: int) -> tuple[list[int], str]:
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
