"""Materialize retry queue entries without calling Bitrix24 or Tomoru."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CallRetryQueueEntry
from app.repositories.call_result_repository import CallResultRepository
from app.services.call_results.idempotency import build_retry_idempotency_key
from app.services.call_results.retry_queue_gateway import RetryQueueGateway
from app.services.phone_service import normalize_phone


@dataclass
class RetryQueueMaterializeReport:
    import_id: int
    created: int = 0
    existing: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    entry_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class RetryQueueMaterializer:
    """Turn prepared retry actions into local queue rows only."""

    def __init__(self, db: Session, portal_id: str):
        self.db = db
        self.portal_id = portal_id
        self.repo = CallResultRepository(db, portal_id)
        self.gateway = RetryQueueGateway(db, portal_id)

    def materialize_import(
        self,
        import_id: int,
        *,
        row_ids: list[int] | None = None,
    ) -> RetryQueueMaterializeReport:
        if self.repo.get_import(import_id) is None:
            raise ValueError("Import not found")

        allowed_rows = set(row_ids or [])
        rows = {row.id: row for row in self.repo.list_rows(import_id)}
        actions = sorted(
            (
                action
                for action in self.repo.list_actions(import_id)
                if (action.operation_type or action.action_type) == "retry_queue_add"
            ),
            key=lambda action: (action.sort_order, action.id),
        )
        report = RetryQueueMaterializeReport(import_id=import_id)

        for action in actions:
            if allowed_rows and action.import_row_id not in allowed_rows:
                continue
            row = rows.get(action.import_row_id)
            if row is None:
                report.errors.append(f"action {action.id}: row not found")
                continue
            if (
                not action.is_enabled
                or action.validation_status == "invalid"
                or row.needs_manual_review
                or row.execution_status == "blocked_manual_review"
            ):
                report.skipped += 1
                continue

            payload = action.payload or {}
            reason = str(payload.get("reason") or "callback_later")
            search_required = bool(payload.get("search_required"))
            alternate = (row.business_signals or {}).get("alternate_contact") or {}
            use_alternate = reason in {"alternate_contact", "hangup_replacement_contact"}
            raw_phone = payload.get("phone")
            if not raw_phone and use_alternate:
                raw_phone = alternate.get("phone")
            if not raw_phone and not search_required:
                raw_phone = row.normalized_phone or row.raw_phone
            phone = normalize_phone(str(raw_phone)) if raw_phone else None
            if not phone and not search_required:
                report.errors.append(f"row {row.id}: no valid phone for retry")
                continue

            extension = payload.get("phone_extension")
            if not extension and use_alternate:
                extension = alternate.get("extension")
            extension = str(extension or row.phone_extension or "").strip() or None
            contact_id = payload.get("contact_id") or row.matched_contact_id
            callback_at = row.callback_at
            timezone_name = str(payload.get("timezone") or "Europe/Moscow")
            status = "scheduled" if callback_at else "ready"
            key = build_retry_idempotency_key(
                portal_id=self.portal_id,
                deal_id=row.matched_deal_id,
                contact_id=contact_id,
                phone=phone,
                source_call_id=row.call_id,
                reason=reason,
                callback_at=callback_at,
            )
            existing = self.db.scalar(
                select(CallRetryQueueEntry).where(
                    CallRetryQueueEntry.portal_id == self.portal_id,
                    CallRetryQueueEntry.idempotency_key == key,
                )
            )
            entry = self.gateway.add(
                import_id=import_id,
                row_id=row.id,
                deal_id=row.matched_deal_id,
                contact_id=contact_id,
                phone_normalized=phone,
                callback_at=callback_at,
                callback_text=(row.business_signals or {}).get("callback_text"),
                reason=reason,
                campaign_id=row.campaign_id,
                source_call_id=row.call_id,
                source_contact_id=row.matched_contact_id,
                replacement_contact_id=payload.get("contact_id") if use_alternate else None,
                search_required=search_required,
                timezone=timezone_name,
                phone_extension=extension,
                status=status,
                attempt_count=row.attempts or 0,
            )
            report.entry_ids.append(entry.id)
            if existing is None:
                report.created += 1
            else:
                report.existing += 1

        self.db.flush()
        return report
