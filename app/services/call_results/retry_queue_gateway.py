"""Retry queue gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallRetryQueueEntry, utcnow
from app.services.call_results.idempotency import build_retry_idempotency_key


class RetryQueueGateway:
    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id

    def add(
        self,
        *,
        import_id: int | None,
        row_id: int | None,
        deal_id: int | None,
        contact_id: int | None,
        phone_normalized: str | None,
        callback_at: datetime | None,
        callback_text: str | None,
        reason: str,
        campaign_id: str | None = None,
        source_call_id: str | None = None,
        source_contact_id: int | None = None,
        replacement_contact_id: int | None = None,
        search_required: bool = False,
        timezone: str | None = None,
        phone_extension: str | None = None,
        attempt_count: int = 0,
        status: str = "ready",
    ) -> CallRetryQueueEntry:
        key = build_retry_idempotency_key(
            portal_id=self.portal_id,
            deal_id=deal_id,
            contact_id=contact_id or replacement_contact_id,
            phone=phone_normalized,
            source_call_id=source_call_id,
            reason=reason,
            callback_at=callback_at,
        )
        existing = self.db.scalar(
            select(CallRetryQueueEntry).where(
                CallRetryQueueEntry.portal_id == self.portal_id,
                CallRetryQueueEntry.idempotency_key == key,
            )
        )
        if existing:
            if phone_extension and not existing.phone_extension:
                existing.phone_extension = phone_extension
            if timezone and not existing.timezone:
                existing.timezone = timezone
            if attempt_count > existing.attempt_count:
                existing.attempt_count = attempt_count
            return existing

        entry = CallRetryQueueEntry(
            portal_id=self.portal_id,
            import_id=import_id,
            row_id=row_id,
            campaign_id=campaign_id,
            source_call_id=source_call_id,
            deal_id=deal_id,
            contact_id=contact_id or replacement_contact_id,
            phone_normalized=phone_normalized,
            phone_extension=phone_extension,
            callback_at=callback_at,
            callback_text=callback_text,
            reason=reason,
            status=status if not search_required else "contact_search_required",
            source_contact_id=source_contact_id,
            replacement_contact_id=replacement_contact_id,
            search_required=search_required,
            idempotency_key=key,
            timezone=timezone,
            attempt_count=max(0, int(attempt_count or 0)),
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
        limit: int | None = 500,
    ) -> list[CallRetryQueueEntry]:
        q = select(CallRetryQueueEntry).where(CallRetryQueueEntry.portal_id == self.portal_id)
        if import_id is not None:
            q = q.where(CallRetryQueueEntry.import_id == import_id)
        if status:
            q = q.where(CallRetryQueueEntry.status == status)
        q = q.order_by(CallRetryQueueEntry.created_at.desc())
        if limit is not None:
            q = q.limit(limit)
        return list(self.db.scalars(q))

    def export_rows(self, entries: list[CallRetryQueueEntry]) -> list[dict[str, Any]]:
        return [
            {
                "id": e.id,
                "import_id": e.import_id,
                "row_id": e.row_id,
                "deal_id": e.deal_id,
                "contact_id": e.contact_id,
                "phone": e.phone_normalized,
                "phone_extension": e.phone_extension or "",
                "callback_at": e.callback_at.isoformat() if e.callback_at else "",
                "callback_text": e.callback_text or "",
                "timezone": e.timezone or "Europe/Moscow",
                "reason": e.reason,
                "status": e.status,
                "campaign_id": e.campaign_id or "",
                "dispatched_campaign_id": e.dispatched_campaign_id or "",
                "source_call_id": e.source_call_id or "",
                "attempt_count": e.attempt_count,
                "last_error": e.last_error or "",
            }
            for e in entries
        ]
