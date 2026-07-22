"""Resolve Bitrix task responsible user for call results."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CallResultImportRow, CrmEntity, ENTITY_DEAL


def resolve_task_responsible_user(
    row: CallResultImportRow,
    settings: Settings,
    portal_id: str,
    db: Session,
    *,
    operator_user_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Return the manager assigned to the matched Bitrix deal.

    ``operator_user_id`` remains in the signature for compatibility with older
    callers, but the operator must not silently replace the deal manager.
    """
    _ = settings, operator_user_id
    if not row.matched_deal_id:
        return None, "Не удалось определить ответственного: у строки нет сделки"

    deal = (
        db.query(CrmEntity)
        .filter(
            CrmEntity.portal_id == portal_id,
            CrmEntity.entity_type_id == ENTITY_DEAL,
            CrmEntity.entity_id == int(row.matched_deal_id),
            CrmEntity.is_deleted.is_(False),
        )
        .one_or_none()
    )
    responsible_id = int(deal.assigned_by_id or 0) if deal else 0
    if responsible_id > 0:
        return responsible_id, None

    return None, "Не удалось определить ответственного менеджера по сделке"


def resolve_task_creator_user(
    settings: Settings,
    *,
    operator_user_id: int | None,
    responsible_user_id: int,
) -> int:
    """Resolve an explicit task creator independently from its assignee."""
    if operator_user_id and operator_user_id > 0:
        return int(operator_user_id)

    service_user_id = int(getattr(settings, "bitrix_service_user_id", 0) or 0)
    if service_user_id > 0:
        return service_user_id

    return int(responsible_user_id)
