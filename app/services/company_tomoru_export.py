"""Build a Tomoru phone list for CRM companies that have no deals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from app.models import (
    ENTITY_COMPANY,
    ENTITY_DEAL,
    CrmContact,
    CrmContactPhone,
    CrmEntity,
)
from app.services.phone_service import extract_phones_from_entity_payload, normalize_phone

QUERY_BATCH_SIZE = 500


@dataclass(frozen=True)
class CompanyPhoneExportResult:
    phones: list[str]
    companies_total: int
    companies_without_deals: int
    companies_with_phones: int


def _chunks(values: list[int], size: int = QUERY_BATCH_SIZE) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _linked_company_ids(db: Session, portal_id: str) -> set[int]:
    """Read only company ids from deal JSON instead of loading every deal object."""
    company_id_value = func.coalesce(
        cast(CrmEntity.raw_payload["COMPANY_ID"].as_string(), String),
        cast(CrmEntity.raw_payload["companyId"].as_string(), String),
    )
    values = db.scalars(
        select(company_id_value).where(
            CrmEntity.portal_id == portal_id,
            CrmEntity.entity_type_id == ENTITY_DEAL,
            CrmEntity.is_deleted.is_(False),
            company_id_value.is_not(None),
        )
    )
    result: set[int] = set()
    for value in values:
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
    company_query = select(CrmEntity.entity_id, CrmEntity.raw_payload).where(
        CrmEntity.portal_id == portal_id,
        CrmEntity.entity_type_id == ENTITY_COMPANY,
        CrmEntity.is_deleted.is_(False),
    )
    if date_from is not None:
        company_query = company_query.where(CrmEntity.created_time >= date_from)
    if date_to is not None:
        company_query = company_query.where(CrmEntity.created_time <= date_to)

    companies = list(db.execute(company_query.order_by(CrmEntity.entity_id.asc())))
    linked_ids = _linked_company_ids(db, portal_id)
    selected = [
        (int(company_id), raw_payload or {})
        for company_id, raw_payload in companies
        if int(company_id) not in linked_ids
    ]

    contacts_by_company: dict[int, list[int]] = defaultdict(list)
    contact_payloads: dict[int, dict] = {}
    selected_ids = [company_id for company_id, _raw_payload in selected]
    for company_ids in _chunks(selected_ids):
        contacts = db.execute(
            select(
                CrmContact.company_id,
                CrmContact.contact_id,
                CrmContact.raw_payload,
            ).where(
                CrmContact.portal_id == portal_id,
                CrmContact.company_id.in_(company_ids),
            )
        )
        for company_id, contact_id, raw_payload in contacts:
            if company_id is None:
                continue
            cid = int(contact_id)
            contacts_by_company[int(company_id)].append(cid)
            contact_payloads[cid] = raw_payload or {}

    contact_phones: dict[int, list[str]] = defaultdict(list)
    contact_ids = list(contact_payloads)
    for contact_id_batch in _chunks(contact_ids):
        phone_rows = db.execute(
            select(CrmContactPhone.contact_id, CrmContactPhone.value).where(
                CrmContactPhone.portal_id == portal_id,
                CrmContactPhone.contact_id.in_(contact_id_batch),
            )
        )
        for contact_id, value in phone_rows:
            contact_phones[int(contact_id)].append(value)

    seen: set[str] = set()
    phones: list[str] = []
    companies_with_phones = 0
    for company_id, company_payload in selected:
        before = len(phones)
        for raw_phone, _normalized_type in extract_phones_from_entity_payload(company_payload):
            normalized = normalize_phone(raw_phone)
            if normalized and normalized not in seen:
                seen.add(normalized)
                phones.append(normalized)
        for contact_id in contacts_by_company.get(company_id, []):
            raw_phones = contact_phones.get(contact_id)
            if raw_phones:
                values = raw_phones
            else:
                values = [
                    raw_phone
                    for raw_phone, _normalized_type in extract_phones_from_entity_payload(
                        contact_payloads.get(contact_id, {})
                    )
                ]
            for value in values:
                normalized = normalize_phone(value)
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
