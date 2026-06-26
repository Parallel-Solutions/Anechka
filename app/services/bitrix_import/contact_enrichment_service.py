"""Обогащение сделок и лидов контактными данными из Bitrix24."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.repositories.contact_repository import ContactRepository
from app.services.bitrix_import.bitrix_crm_client import BitrixCrmClient
from app.services.bitrix_import.contact_parser import extract_contact_fields

logger = logging.getLogger(__name__)


class ContactEnrichmentService:
    def __init__(self, db: Session, portal_id: str, client: BitrixCrmClient):
        self.db = db
        self.portal_id = portal_id
        self.client = client
        self.repo = ContactRepository(db, portal_id)

    def enrich_deal(self, deal_id: int, payload: dict[str, Any] | None = None) -> int:
        items = self.client.get_deal_contact_items(deal_id)
        return self.enrich_deal_with_items(deal_id, items, payload)

    def enrich_deal_with_items(
        self,
        deal_id: int,
        items: list[dict],
        payload: dict[str, Any] | None = None,
    ) -> int:
        count = self._process_links(items, parent_type=2, parent_id=deal_id)
        company_id = self._company_id_from_payload(payload) if payload else None
        if company_id:
            count += self.enrich_company_contacts(company_id)
        return count

    def enrich_lead(self, lead_id: int, lead_payload: dict[str, Any]) -> int:
        items = self.client.get_lead_contact_items(lead_id)
        return self.enrich_lead_with_items(lead_id, items, lead_payload)

    def enrich_lead_with_items(self, lead_id: int, items: list[dict], payload: dict[str, Any]) -> int:
        if items:
            return self._process_links(items, parent_type=1, parent_id=lead_id)
        self._enrich_lead_from_fields(lead_id, payload)
        return 1

    def enrich_company_contacts(self, company_id: int) -> int:
        """Import contacts linked to a company (not necessarily linked to a deal)."""
        if not company_id:
            return 0
        try:
            contact_ids = self.client.get_company_contacts(company_id)
        except Exception:
            logger.exception("Failed to fetch contacts for company %s", company_id)
            return 0
        count = 0
        for cid in contact_ids:
            self._save_contact(cid)
            count += 1
        return count

    @staticmethod
    def _company_id_from_payload(payload: dict[str, Any]) -> int | None:
        val = payload.get("companyId") or payload.get("COMPANY_ID")
        if val in (None, "", "0", 0):
            return None
        try:
            cid = int(val)
            return cid if cid > 0 else None
        except (TypeError, ValueError):
            return None

    def _process_links(self, items: list[dict], parent_type: int, parent_id: int) -> int:
        count = 0
        primary_id = self._primary_contact_id(items)
        for it in items:
            cid = it.get("CONTACT_ID") or it.get("contactId")
            if not cid:
                continue
            cid = int(cid)
            self._save_contact(cid)
            self.repo.upsert_link(cid, parent_type, parent_id, is_primary=(cid == primary_id))
            count += 1
        return count

    @staticmethod
    def _primary_contact_id(items: list[dict]) -> int | None:
        if not items:
            return None
        for it in items:
            if str(it.get("IS_PRIMARY", "")).upper() == "Y":
                return int(it.get("CONTACT_ID"))
        def sort_key(it: dict) -> int:
            try:
                return int(it.get("SORT", 0))
            except (TypeError, ValueError):
                return 0

        ordered = sorted(items, key=sort_key)
        return int(ordered[0].get("CONTACT_ID"))

    def _save_contact(self, contact_id: int) -> None:
        data = self.client.safe_call("crm.contact.get", {"id": contact_id})
        contact = (data or {}).get("result") if data else None
        if not contact:
            logger.warning("Contact %s not found, skipping save", contact_id)
            return
        fields = extract_contact_fields(contact)
        if fields["company_id"]:
            comp_data = self.client.safe_call("crm.company.get", {"id": fields["company_id"]})
            company = (comp_data or {}).get("result") if comp_data else None
            if company:
                fields["company_title"] = (
                    company.get("TITLE") or company.get("title") or fields["company_title"]
                )
        self.repo.upsert_contact(contact_id, fields, raw_payload=contact)
        self.repo.sync_phones(contact_id, fields["phones"], fields["primary_phone"])

    def _enrich_lead_from_fields(self, lead_id: int, payload: dict[str, Any]) -> None:
        synthetic_id = -lead_id
        fields = extract_contact_fields(payload)
        if fields["company_id"]:
            company = self.client.get_company(fields["company_id"])
            if company:
                fields["company_title"] = (
                    company.get("TITLE") or company.get("title") or fields["company_title"]
                )
        self.repo.upsert_contact(
            synthetic_id,
            fields,
            is_synthetic=True,
            source_lead_id=lead_id,
            raw_payload=payload,
        )
        self.repo.sync_phones(synthetic_id, fields["phones"], fields["primary_phone"])
        self.repo.upsert_link(synthetic_id, 1, lead_id, is_primary=True)
