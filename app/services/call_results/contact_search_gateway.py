"""Contact search queue gateway."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallContactSearchEntry, CallResultImportRow, utcnow


class ContactSearchProvider(Protocol):
    def find_candidate(self, *, deal_id: int, exclude_phone: str | None, deal_contact_ids: list[int]) -> dict | None: ...


class FakeContactSearchProvider:
    def find_candidate(self, *, deal_id: int, exclude_phone: str | None, deal_contact_ids: list[int]) -> dict | None:
        for cid in deal_contact_ids:
            return {"contact_id": cid, "phone": None, "confidence": 0.5}
        return None


class ContactSearchGateway:
    """Gateway for hangup replacement contact search queue.

    TODO(contact-search): implement real ContactSearchProvider (CrmContactLink / Bitrix API),
    wire UI confirm button, and export retry queue to Tomoru (sent_to_tomoru).
    """
    def __init__(self, db: Session, portal_id: str, provider: ContactSearchProvider | None = None):
        self.db = db
        self.portal_id = portal_id
        self.provider = provider or FakeContactSearchProvider()

    def create_from_row(self, row: CallResultImportRow, *, deal_contact_ids: list[int] | None = None) -> CallContactSearchEntry:
        nd = row.normalized_data or {}
        entry = CallContactSearchEntry(
            portal_id=self.portal_id,
            import_id=row.import_id,
            row_id=row.id,
            deal_id=row.matched_deal_id,
            company_id=row.matched_company_id,
            region=nd.get("region"),
            source_phone=row.normalized_phone,
            source_contact_id=row.matched_contact_id,
            deal_contact_ids=deal_contact_ids or [],
            summary=(row.business_signals or {}).get("summary") or row.comment,
            call_id=row.call_id,
            campaign_id=row.campaign_id,
            previous_attempts=row.attempts or 0,
            status="contact_search_required",
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self.db.add(entry)
        self.db.flush()
        return entry

    def list_entries(
        self,
        *,
        import_id: int | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> list[CallContactSearchEntry]:
        q = select(CallContactSearchEntry).where(CallContactSearchEntry.portal_id == self.portal_id)
        if import_id is not None:
            q = q.where(CallContactSearchEntry.import_id == import_id)
        if status:
            q = q.where(CallContactSearchEntry.status == status)
        q = q.order_by(CallContactSearchEntry.created_at.desc()).limit(limit)
        return list(self.db.scalars(q))

    def confirm_contact(
        self,
        entry_id: int,
        *,
        contact_id: int,
        phone: str | None,
        confirmed_by: str,
    ) -> CallContactSearchEntry | None:
        entry = self.db.get(CallContactSearchEntry, entry_id)
        if entry is None or entry.portal_id != self.portal_id:
            return None
        entry.found_contact_id = contact_id
        entry.found_contact_phone = phone
        entry.confirmed_by = confirmed_by
        entry.confirmed_at = utcnow()
        entry.status = "contact_confirmed"
        entry.updated_at = utcnow()
        self.db.flush()
        return entry

    def auto_search(self, entry: CallContactSearchEntry) -> dict[str, Any] | None:
        entry.status = "searching"
        ids = entry.deal_contact_ids or []
        cand = self.provider.find_candidate(
            deal_id=entry.deal_id or 0,
            exclude_phone=entry.source_phone,
            deal_contact_ids=[int(x) for x in ids if x],
        )
        if cand:
            entry.status = "candidate_found"
            entry.found_contact_id = cand.get("contact_id")
            entry.found_contact_phone = cand.get("phone")
        else:
            entry.status = "no_contact_found"
        entry.updated_at = utcnow()
        self.db.flush()
        return cand
