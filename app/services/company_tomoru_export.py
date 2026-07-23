"""Build a Tomoru phone list for CRM companies that have no deals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import ENTITY_COMPANY, ENTITY_DEAL
from app.repositories.contact_repository import ContactRepository
from app.repositories.crm_repository import CrmRepository
from app.services.export_plan.payload_keys import payload_lookup
from app.services.phone_service import extract_phones_from_entity_payload, normalize_phone


@dataclass(frozen=True)
class CompanyPhoneExportResult:
    phones: list[str]
    companies_total: int
    companies_without_deals: int
    companies_with_phones: int


def _linked_company_ids(deals) -> set[int]:
    result: set[int] = set()
    for deal in deals:
        raw = deal.raw_payload or {}
        value = payload_lookup(raw, "COMPANY_ID") or payload_lookup(raw, "companyId")
        try:
            company_id = int(value)
        except (TypeError, ValueError):
            continue
        if company_id > 0:
            result.add(company_id)
    return result


def build_companies_without_deals_export(
    db: Session,
    portal_id: str,
    *,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> CompanyPhoneExportResult:
    crm_repo = CrmRepository(db, portal_id)
    contact_repo = ContactRepository(db, portal_id)
    companies = crm_repo.list_entities_for_export(
        ENTITY_COMPANY,
        date_from=date_from,
        date_to=date_to,
    )
    deals = crm_repo.list_entities_for_export(ENTITY_DEAL)
    linked_ids = _linked_company_ids(deals)
    selected = [company for company in companies if int(company.entity_id) not in linked_ids]
    contacts_by_company = contact_repo.get_contacts_by_company_ids(
        [int(company.entity_id) for company in selected]
    )
    contact_ids = [
        int(contact.contact_id)
        for contacts in contacts_by_company.values()
        for contact in contacts
    ]
    contact_phones = contact_repo.get_phones_for_contacts(contact_ids)

    seen: set[str] = set()
    phones: list[str] = []
    companies_with_phones = 0
    for company in selected:
        company_id = int(company.entity_id)
        before = len(phones)
        for _raw, normalized_type in extract_phones_from_entity_payload(company.raw_payload or {}):
            normalized = normalize_phone(_raw)
            if normalized and normalized not in seen:
                seen.add(normalized)
                phones.append(normalized)
        for contact in contacts_by_company.get(company_id, []):
            for phone_row in contact_phones.get(int(contact.contact_id), []):
                normalized = normalize_phone(phone_row.get("value") or "")
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    phones.append(normalized)
        if len(phones) > before:
            companies_with_phones += 1

    return CompanyPhoneExportResult(
        phones=phones,
        companies_total=len(companies),
        companies_without_deals=len(selected),
        companies_with_phones=companies_with_phones,
    )
