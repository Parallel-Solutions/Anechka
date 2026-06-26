"""Data-readiness checks for intelligent export.

A common cause of "ничего не выгружается" is the app being connected to a
database that has no imported CRM data (e.g. a stray host instance pointing at
an empty DB). These helpers make that state explicit so the UI and the planner
can fail fast with a clear, actionable message instead of spinning.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.bitrix import CrmEntity, CrmFieldDefinition
from app.services.intelligent_export.staleness import SyncState, compute_sync_state


@dataclass
class Readiness:
    portal_id: str
    crm_entities: int
    field_definitions: int
    sync_state: SyncState

    @property
    def has_data(self) -> bool:
        return self.crm_entities > 0

    @property
    def ready(self) -> bool:
        return self.has_data and self.sync_state.export_allowed

    def to_dict(self) -> dict:
        return {
            "portal_id": self.portal_id,
            "crm_entities": self.crm_entities,
            "field_definitions": self.field_definitions,
            "has_data": self.has_data,
            "ready": self.ready,
            "sync_state": self.sync_state.to_dict(),
        }


def compute_readiness(db: Session, portal_id: str, settings: Settings) -> Readiness:
    crm_entities = db.scalar(
        select(func.count()).select_from(CrmEntity).where(CrmEntity.portal_id == portal_id)
    ) or 0
    field_definitions = db.scalar(
        select(func.count())
        .select_from(CrmFieldDefinition)
        .where(CrmFieldDefinition.portal_id == portal_id)
    ) or 0
    sync_state = compute_sync_state(db, portal_id, settings)
    return Readiness(
        portal_id=portal_id,
        crm_entities=crm_entities,
        field_definitions=field_definitions,
        sync_state=sync_state,
    )
