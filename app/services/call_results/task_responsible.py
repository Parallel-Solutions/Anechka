"""Resolve Bitrix task responsible user for call results."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CallResultImportRow


def resolve_task_responsible_user(
    row: CallResultImportRow,
    settings: Settings,
    portal_id: str,
    db: Session,
    *,
    operator_user_id: int | None = None,
) -> tuple[int | None, str | None]:
    """Return (user_id, error_message). Operator Bitrix ID first, then BITRIX_SERVICE_USER_ID."""
    _ = row, portal_id, db
    if operator_user_id and operator_user_id > 0:
        return operator_user_id, None

    service_user_id = int(getattr(settings, "bitrix_service_user_id", 0) or 0)
    if service_user_id > 0:
        return service_user_id, None

    return None, (
        "Не удалось определить ответственного: "
        "нет Bitrix ID у пользователя и BITRIX_SERVICE_USER_ID"
    )
