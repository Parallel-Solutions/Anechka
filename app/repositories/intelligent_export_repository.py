"""Persistence for the intelligent export subsystem.

Every method is scoped: it requires the acting user and portal so a caller can
never read or mutate another user's conversation/plan/run. Ownership and
portal are checked on every access (no bare ``get_by_id``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AppUser,
    IeConversation,
    IeExportPlanVersion,
    IeExportRun,
    IeMemoryEntry,
    IeMessage,
    utcnow,
)


class IeNotFound(Exception):
    code = "NOT_FOUND"


class IeAccessDenied(Exception):
    code = "ACCESS_DENIED"


class PlanVersionConflict(Exception):
    code = "PLAN_VERSION_CONFLICT"


def plan_hash(plan_json: dict) -> str:
    canonical = json.dumps(plan_json, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ScopeContext:
    user: AppUser
    portal_id: str

    @property
    def user_id(self) -> int:
        return self.user.id

    @property
    def is_admin(self) -> bool:
        return self.user.role == "admin"


class IntelligentExportRepository:
    def __init__(self, db: Session, scope: ScopeContext):
        self.db = db
        self.scope = scope

    # --- conversations ------------------------------------------------------
    def create_conversation(self, title: str = "") -> IeConversation:
        conv = IeConversation(
            portal_id=self.scope.portal_id,
            user_id=self.scope.user_id,
            title=title or "Новый диалог",
            status="active",
        )
        self.db.add(conv)
        self.db.commit()
        self.db.refresh(conv)
        return conv

    def list_conversations(self, include_archived: bool = False) -> list[IeConversation]:
        stmt = select(IeConversation).where(
            IeConversation.portal_id == self.scope.portal_id,
            IeConversation.user_id == self.scope.user_id,
        )
        if not include_archived:
            stmt = stmt.where(IeConversation.status == "active")
        stmt = stmt.order_by(IeConversation.updated_at.desc())
        return list(self.db.scalars(stmt))

    def get_conversation(self, conversation_id: int) -> IeConversation:
        conv = self.db.get(IeConversation, conversation_id)
        if conv is None or conv.portal_id != self.scope.portal_id:
            raise IeNotFound("conversation not found")
        if conv.user_id != self.scope.user_id and not self.scope.is_admin:
            raise IeAccessDenied("not your conversation")
        return conv

    def update_conversation(self, conversation_id: int, *, title: str | None = None, status: str | None = None) -> IeConversation:
        conv = self.get_conversation(conversation_id)
        if title is not None:
            conv.title = title[:255]
        if status is not None:
            conv.status = status
        conv.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(conv)
        return conv

    def archive_conversation(self, conversation_id: int) -> IeConversation:
        return self.update_conversation(conversation_id, status="archived")

    def touch_conversation(self, conv: IeConversation) -> None:
        conv.updated_at = utcnow()
        self.db.commit()

    # --- messages -----------------------------------------------------------
    def add_message(self, conversation_id: int, role: str, content: str, metadata: dict | None = None) -> IeMessage:
        # ownership check
        self.get_conversation(conversation_id)
        msg = IeMessage(conversation_id=conversation_id, role=role, content=content, metadata_json=metadata)
        self.db.add(msg)
        self.db.commit()
        self.db.refresh(msg)
        return msg

    def list_messages(self, conversation_id: int) -> list[IeMessage]:
        self.get_conversation(conversation_id)
        stmt = select(IeMessage).where(IeMessage.conversation_id == conversation_id).order_by(IeMessage.id)
        return list(self.db.scalars(stmt))

    # --- plan versions (immutable) -----------------------------------------
    def save_plan_version(
        self,
        conversation_id: int,
        *,
        plan_json: dict,
        validation_result_json: dict | None,
        catalog_snapshot_hash: str,
        expected_current_version_number: int | None = None,
    ) -> IeExportPlanVersion:
        """Append an immutable plan version.

        Version status (draft/valid/invalid/superseded/archived) and applied
        memory are stored inside ``validation_result_json`` to avoid widening
        the table; old versions are never mutated.
        """
        conv = self.get_conversation(conversation_id)
        current_max = self.db.scalar(
            select(func.max(IeExportPlanVersion.version_number)).where(
                IeExportPlanVersion.conversation_id == conversation_id
            )
        )
        current_max = current_max or 0

        if expected_current_version_number is not None and expected_current_version_number != current_max:
            raise PlanVersionConflict(
                f"expected version {expected_current_version_number}, current is {current_max}"
            )

        version = IeExportPlanVersion(
            conversation_id=conversation_id,
            version_number=current_max + 1,
            plan_json=plan_json,
            plan_hash=plan_hash(plan_json),
            validation_result_json=validation_result_json,
            catalog_snapshot_hash=catalog_snapshot_hash,
            created_by_user_id=self.scope.user_id,
        )
        self.db.add(version)
        self.db.flush()
        conv.current_plan_version_id = version.id
        conv.updated_at = utcnow()
        if not conv.title or conv.title == "Новый диалог":
            conv.title = (plan_json.get("title") or conv.title)[:255]
        self.db.commit()
        self.db.refresh(version)
        return version

    def get_plan_version(self, plan_version_id: int) -> IeExportPlanVersion:
        version = self.db.get(IeExportPlanVersion, plan_version_id)
        if version is None:
            raise IeNotFound("plan version not found")
        # ownership via conversation
        self.get_conversation(version.conversation_id)
        return version

    def list_plan_versions(self, conversation_id: int) -> list[IeExportPlanVersion]:
        self.get_conversation(conversation_id)
        stmt = (
            select(IeExportPlanVersion)
            .where(IeExportPlanVersion.conversation_id == conversation_id)
            .order_by(IeExportPlanVersion.version_number)
        )
        return list(self.db.scalars(stmt))

    def activate_plan_version(self, plan_version_id: int) -> IeExportPlanVersion:
        version = self.get_plan_version(plan_version_id)
        conv = self.get_conversation(version.conversation_id)
        conv.current_plan_version_id = version.id
        conv.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(version)
        return version

    def clone_plan_to_new_conversation(self, plan_version_id: int) -> tuple[IeConversation, IeExportPlanVersion]:
        source = self.get_plan_version(plan_version_id)
        plan_copy = dict(source.plan_json)
        conv = self.create_conversation(title=plan_copy.get("title", "Копия плана"))
        version = self.save_plan_version(
            conv.id,
            plan_json=plan_copy,
            validation_result_json=source.validation_result_json,
            catalog_snapshot_hash=source.catalog_snapshot_hash,
        )
        return conv, version

    # --- runs ---------------------------------------------------------------
    def create_run(
        self,
        *,
        plan_version_id: int,
        conversation_id: int | None,
        status: str = "preview",
        export_job_id: int | None = None,
    ) -> IeExportRun:
        run = IeExportRun(
            portal_id=self.scope.portal_id,
            user_id=self.scope.user_id,
            conversation_id=conversation_id,
            plan_version_id=plan_version_id,
            export_job_id=export_job_id,
            status=status,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def get_run(self, run_id: int) -> IeExportRun:
        run = self.db.get(IeExportRun, run_id)
        if run is None or run.portal_id != self.scope.portal_id:
            raise IeNotFound("run not found")
        if run.user_id != self.scope.user_id and not self.scope.is_admin:
            raise IeAccessDenied("not your run")
        return run

    def list_runs(self) -> list[IeExportRun]:
        stmt = (
            select(IeExportRun)
            .where(IeExportRun.portal_id == self.scope.portal_id, IeExportRun.user_id == self.scope.user_id)
            .order_by(IeExportRun.id.desc())
        )
        return list(self.db.scalars(stmt))

    def update_run(self, run_id: int, **fields) -> IeExportRun:
        run = self.get_run(run_id)
        for key, value in fields.items():
            setattr(run, key, value)
        self.db.commit()
        self.db.refresh(run)
        return run

    # --- memory -------------------------------------------------------------
    def list_memory(
        self,
        *,
        scope: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        include_inactive: bool = False,
    ) -> list[IeMemoryEntry]:
        stmt = select(IeMemoryEntry).where(IeMemoryEntry.portal_id == self.scope.portal_id)
        # visibility: project memory is visible to all; user memory only to owner
        stmt = stmt.where(
            (IeMemoryEntry.scope == "project")
            | ((IeMemoryEntry.scope == "user") & (IeMemoryEntry.user_id == self.scope.user_id))
        )
        if scope:
            stmt = stmt.where(IeMemoryEntry.scope == scope)
        if kind:
            stmt = stmt.where(IeMemoryEntry.kind == kind)
        if status:
            stmt = stmt.where(IeMemoryEntry.status == status)
        if not include_inactive:
            stmt = stmt.where(IeMemoryEntry.is_active.is_(True))
        stmt = stmt.order_by(IeMemoryEntry.priority.desc(), IeMemoryEntry.updated_at.desc())
        return list(self.db.scalars(stmt))

    def find_memory(
        self, *, scope: str, kind: str, key: str
    ) -> IeMemoryEntry | None:
        """Locate a single memory entry by its natural key within the portal.

        Used for idempotent re-generation: includes inactive/non-approved rows so
        a prior decision (approved/rejected/archived) is never silently bypassed.
        """
        stmt = select(IeMemoryEntry).where(
            IeMemoryEntry.portal_id == self.scope.portal_id,
            IeMemoryEntry.scope == scope,
            IeMemoryEntry.kind == kind,
            IeMemoryEntry.key == key[:255],
        )
        if scope == "user":
            stmt = stmt.where(IeMemoryEntry.user_id == self.scope.user_id)
        stmt = stmt.order_by(IeMemoryEntry.id.desc())
        return self.db.scalars(stmt).first()

    def get_memory(self, memory_id: int) -> IeMemoryEntry:
        entry = self.db.get(IeMemoryEntry, memory_id)
        if entry is None or entry.portal_id != self.scope.portal_id:
            raise IeNotFound("memory not found")
        if entry.scope == "user" and entry.user_id != self.scope.user_id and not self.scope.is_admin:
            raise IeAccessDenied("not your memory")
        return entry

    def create_memory(
        self,
        *,
        scope: str,
        kind: str,
        key: str,
        content: str | None,
        value_json: dict | None,
        status: str,
        source: str = "manual",
        priority: int = 100,
        source_conversation_id: int | None = None,
        source_message_id: int | None = None,
    ) -> IeMemoryEntry:
        entry = IeMemoryEntry(
            portal_id=self.scope.portal_id,
            scope=scope,
            user_id=self.scope.user_id if scope == "user" else None,
            kind=kind,
            key=key[:255],
            content=content,
            value_json=value_json,
            status=status,
            source=source,
            priority=priority,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            valid_from=datetime.now(timezone.utc),
            is_active=True,
            created_by_user_id=self.scope.user_id,
        )
        if status == "approved":
            entry.approved_by_user_id = self.scope.user_id
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def update_memory(self, memory_id: int, **fields) -> IeMemoryEntry:
        entry = self.get_memory(memory_id)
        for key, value in fields.items():
            setattr(entry, key, value)
        entry.version += 1
        entry.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def soft_delete_memory(self, memory_id: int) -> IeMemoryEntry:
        entry = self.get_memory(memory_id)
        entry.is_active = False
        entry.status = "archived"
        entry.deleted_at = datetime.now(timezone.utc)
        entry.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(entry)
        return entry
