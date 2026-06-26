"""Import freshness policy for intelligent export (ADR-002).

Reads the latest successful sync from ``sync_checkpoints`` and classifies the
state as normal / warning / blocked using admin-configurable thresholds. Full
export is blocked when data is critically stale; preview is allowed with a
warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import SyncCheckpoint


@dataclass
class SyncState:
    state: str  # normal | warning | blocked
    last_successful_sync_at: datetime | None
    age_hours: float | None
    warn_hours: int
    block_hours: int

    @property
    def export_allowed(self) -> bool:
        return self.state != "blocked"

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "last_successful_sync_at": self.last_successful_sync_at.isoformat() if self.last_successful_sync_at else None,
            "age_hours": round(self.age_hours, 2) if self.age_hours is not None else None,
            "warn_hours": self.warn_hours,
            "block_hours": self.block_hours,
            "export_allowed": self.export_allowed,
        }


def compute_sync_state(db: Session, portal_id: str, settings: Settings) -> SyncState:
    warn = settings.ie_staleness_warn_hours
    block = settings.ie_staleness_block_hours
    last = db.scalar(
        select(func.max(SyncCheckpoint.last_successful_sync_at)).where(SyncCheckpoint.portal_id == portal_id)
    )
    if last is None:
        return SyncState(state="blocked", last_successful_sync_at=None, age_hours=None, warn_hours=warn, block_hours=block)

    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    if age_hours >= block:
        state = "blocked"
    elif age_hours >= warn:
        state = "warning"
    else:
        state = "normal"
    return SyncState(state=state, last_successful_sync_at=last, age_hours=age_hours, warn_hours=warn, block_hours=block)
