"""Periodic Bitrix CRM import scheduler (runs inside worker)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import utcnow
from app.repositories.sync_repository import SyncRepository
from app.services.bitrix_import.import_queue_service import ConcurrentImportError, enqueue_import

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class BitrixImportScheduler:
    def maybe_enqueue(self, db: Session, settings: Settings, portal_id: str) -> bool:
        if not settings.bitrix_import_schedule_enabled:
            return False

        if not settings.bitrix_webhook_url:
            return False

        sync_repo = SyncRepository(db)
        interval = timedelta(minutes=settings.bitrix_import_schedule_interval_minutes)

        last = sync_repo.last_successful_run(portal_id, "incremental")
        if last is None:
            last = sync_repo.last_successful_run(portal_id, "full")

        if last and last.finished_at and utcnow() - _as_utc(last.finished_at) < interval:
            return False

        if sync_repo.has_active_run(portal_id):
            logger.info("Scheduled import skipped: active run for portal %s", portal_id)
            return False

        try:
            run = enqueue_import(
                sync_repo,
                portal_id,
                mode="incremental",
                requested_by="scheduler",
                analyze_metadata=True,
            )
        except ConcurrentImportError:
            logger.info("Scheduled import skipped: concurrent run for portal %s", portal_id)
            return False

        logger.info(
            "Scheduled import enqueued: run_id=%s mode=%s portal=%s",
            run.id,
            run.mode,
            portal_id,
        )
        return True
