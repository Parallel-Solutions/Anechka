"""Idempotency key builders for call results operations."""

from __future__ import annotations

from datetime import datetime


def build_action_idempotency_key(
    *,
    method: str,
    deal_id: int | None,
    source_id: str,
    operation_type: str,
    extra: str = "",
) -> str:
    parts = [method, str(deal_id or 0), source_id, operation_type]
    if extra:
        parts.append(extra)
    return ":".join(parts)


def build_retry_idempotency_key(
    *,
    portal_id: str,
    deal_id: int | None,
    contact_id: int | None,
    phone: str | None,
    source_call_id: str | None,
    reason: str,
    callback_at: datetime | None,
) -> str:
    cb = callback_at.isoformat() if callback_at else "none"
    ident = str(contact_id) if contact_id else (phone or "no_phone")
    return f"{portal_id}:{deal_id or 0}:{ident}:{source_call_id or 'no_call'}:{reason}:{cb}"
