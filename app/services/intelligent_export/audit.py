"""Audit logging and retention for the intelligent export subsystem.

Audit entries record security-relevant actions (login, plan save, export run,
memory approval, download). Retention purges old runs/audit rows beyond a
configurable horizon.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import IeAuditLog, IeExportRun

logger = logging.getLogger(__name__)


def audit(
    db: Session,
    portal_id: str,
    action: str,
    *,
    user_id: int | None = None,
    object_type: str | None = None,
    object_id: str | int | None = None,
    detail: dict | None = None,
) -> None:
    try:
        entry = IeAuditLog(
            portal_id=portal_id,
            user_id=user_id,
            action=action,
            object_type=object_type,
            object_id=str(object_id) if object_id is not None else None,
            detail_json=detail,
        )
        db.add(entry)
        db.commit()
    except Exception:  # noqa: BLE001 — audit must never break the request
        db.rollback()
        logger.exception("Failed to write audit log: %s", action)


def list_audit(db: Session, portal_id: str, *, limit: int = 200) -> list[IeAuditLog]:
    stmt = (
        select(IeAuditLog)
        .where(IeAuditLog.portal_id == portal_id)
        .order_by(IeAuditLog.id.desc())
        .limit(limit)
    )
    return list(db.scalars(stmt))


def purge_old_runs(db: Session, portal_id: str, *, days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    runs = list(
        db.scalars(
            select(IeExportRun).where(
                IeExportRun.portal_id == portal_id, IeExportRun.created_at < cutoff
            )
        )
    )
    for run in runs:
        db.delete(run)
    db.commit()
    return len(runs)


def purge_old_audit(db: Session, portal_id: str, *, days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(
        delete(IeAuditLog).where(IeAuditLog.portal_id == portal_id, IeAuditLog.created_at < cutoff)
    )
    db.commit()
    return result.rowcount or 0
