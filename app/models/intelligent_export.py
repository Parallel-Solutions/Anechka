"""SQLAlchemy models — intelligent export subsystem."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.db.types import JSONType
from app.models.legacy import utcnow

APP_ROLES = ("admin", "analyst", "viewer")
MEMORY_SCOPES = ("project", "user")
MEMORY_KINDS = ("term", "alias", "mapping", "template", "rule", "instruction", "preference")
MEMORY_STATUSES = ("proposed", "approved", "rejected", "deprecated", "archived")
MEMORY_SOURCES = ("manual", "ai_proposed", "import")
CONVERSATION_STATUSES = ("active", "archived")
EXPORT_RUN_STATUSES = ("preview", "queued", "running", "completed", "failed", "cancelled")


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    email: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    crm_user_external_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "email", name="uq_app_user_email"),
    )


class IeConversation(Base):
    __tablename__ = "ie_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    current_plan_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_ie_conversations_user_updated", "portal_id", "user_id", "updated_at"),
    )


class IeMessage(Base):
    __tablename__ = "ie_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_ie_messages_conversation", "conversation_id", "id"),
    )


class IeExportPlanVersion(Base):
    __tablename__ = "ie_export_plan_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(Integer, index=True)
    version_number: Mapped[int] = mapped_column(Integer)
    plan_json: Mapped[dict] = mapped_column(JSONType, default=dict)
    plan_hash: Mapped[str] = mapped_column(String(64), default="")
    validation_result_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    catalog_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    created_by_user_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("conversation_id", "version_number", name="uq_ie_plan_version"),
    )


class IeMemoryEntry(Base):
    __tablename__ = "ie_memory_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    scope: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(255))
    value_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="approved")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    source: Mapped[str] = mapped_column(String(16), default="manual")
    source_conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_ie_memory_lookup", "portal_id", "scope", "kind", "key"),
        Index("ix_ie_memory_status", "portal_id", "scope", "status"),
    )


class IeExportRun(Base):
    __tablename__ = "ie_export_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    conversation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_version_id: Mapped[int] = mapped_column(Integer, index=True)
    export_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="preview")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    error_row_count: Mapped[int] = mapped_column(Integer, default=0)
    result_summary_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IeAuditLog(Base):
    __tablename__ = "ie_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    object_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_ie_audit_lookup", "portal_id", "action", "created_at"),)
