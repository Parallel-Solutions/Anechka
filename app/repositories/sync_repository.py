"""Repository for sync runs and checkpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import SyncCheckpoint, SyncRun, utcnow


class SyncRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_run(
        self,
        portal_id: str,
        mode: str,
        requested_by: str | None = None,
        statistics: dict | None = None,
    ) -> SyncRun:
        run = SyncRun(
            portal_id=portal_id,
            mode=mode,
            status="pending",
            requested_by=requested_by,
            statistics_json=statistics or {},
            current_phase="queued",
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def get_run(self, run_id: int) -> SyncRun | None:
        return self.db.get(SyncRun, run_id)

    def list_runs(self, portal_id: str | None = None, limit: int = 50) -> list[SyncRun]:
        q = select(SyncRun).order_by(SyncRun.created_at.desc()).limit(limit)
        if portal_id:
            q = q.where(SyncRun.portal_id == portal_id)
        return list(self.db.scalars(q))

    def has_active_run(self, portal_id: str, mode: str | None = None) -> bool:
        q = select(SyncRun).where(
            SyncRun.portal_id == portal_id,
            SyncRun.status.in_(["pending", "running"]),
        )
        if mode:
            q = q.where(SyncRun.mode == mode)
        return self.db.scalar(q.limit(1)) is not None

    def claim_next_run(self) -> SyncRun | None:
        stmt = (
            select(SyncRun)
            .where(SyncRun.status == "pending")
            .order_by(SyncRun.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        run = self.db.scalar(stmt)
        if not run:
            return None
        run.status = "running"
        run.started_at = utcnow()
        run.heartbeat_at = utcnow()
        run.current_phase = "starting"
        self.db.commit()
        self.db.refresh(run)
        return run

    def heartbeat(self, run_id: int) -> None:
        self.db.execute(
            update(SyncRun)
            .where(SyncRun.id == run_id)
            .values(heartbeat_at=utcnow(), updated_at=utcnow())
        )
        self.db.commit()

    def update_progress(self, run_id: int, **kwargs: Any) -> None:
        run = self.get_run(run_id)
        if not run:
            return
        for key, value in kwargs.items():
            if hasattr(run, key):
                setattr(run, key, value)
        run.updated_at = utcnow()
        self.db.commit()

    def complete_run(self, run_id: int, status: str = "completed", error: str | None = None) -> None:
        run = self.get_run(run_id)
        if not run:
            return
        run.status = status
        run.finished_at = utcnow()
        run.updated_at = utcnow()
        if error:
            run.last_error = error
        self.db.commit()

    def cancel_run(self, run_id: int) -> SyncRun | None:
        run = self.get_run(run_id)
        if not run:
            return None
        if run.status in ("completed", "failed", "cancelled"):
            return run
        run.cancel_requested = True
        if run.status == "pending":
            run.status = "cancelled"
            run.finished_at = utcnow()
        self.db.commit()
        self.db.refresh(run)
        return run

    def recover_stale_runs(self, stale_minutes: int) -> int:
        cutoff = utcnow() - timedelta(minutes=stale_minutes)
        stale = list(
            self.db.scalars(
                select(SyncRun).where(
                    SyncRun.status == "running",
                    SyncRun.heartbeat_at < cutoff,
                )
            )
        )
        for run in stale:
            run.status = "failed"
            run.last_error = "Зависшее задание восстановлено worker-ом"
            run.finished_at = utcnow()
        if stale:
            self.db.commit()
        return len(stale)

    def get_checkpoint(
        self, portal_id: str, resource_name: str, entity_type_id: int = 0
    ) -> SyncCheckpoint | None:
        return self.db.scalar(
            select(SyncCheckpoint).where(
                SyncCheckpoint.portal_id == portal_id,
                SyncCheckpoint.resource_name == resource_name,
                SyncCheckpoint.entity_type_id == entity_type_id,
            )
        )

    def upsert_checkpoint(
        self,
        portal_id: str,
        resource_name: str,
        entity_type_id: int,
        cursor_time: datetime | None,
        cursor_id: int | None,
        metadata: dict | None = None,
    ) -> SyncCheckpoint:
        cp = self.get_checkpoint(portal_id, resource_name, entity_type_id)
        now = utcnow()
        if cp is None:
            cp = SyncCheckpoint(
                portal_id=portal_id,
                resource_name=resource_name,
                entity_type_id=entity_type_id,
            )
            self.db.add(cp)
        cp.cursor_time = cursor_time
        cp.cursor_id = cursor_id
        cp.last_successful_sync_at = now
        cp.metadata_json = metadata or cp.metadata_json
        cp.updated_at = now
        self.db.commit()
        self.db.refresh(cp)
        return cp

    def last_successful_run(self, portal_id: str, mode: str) -> SyncRun | None:
        return self.db.scalar(
            select(SyncRun)
            .where(
                SyncRun.portal_id == portal_id,
                SyncRun.mode == mode,
                SyncRun.status == "completed",
            )
            .order_by(SyncRun.finished_at.desc())
            .limit(1)
        )
