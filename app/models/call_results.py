"""Models for call result import and Bitrix action preparation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.db.types import JSONType
from app.models.legacy import utcnow

IMPORT_STATUSES = ("uploaded", "processing", "ready", "failed")
MATCH_STATUSES = (
    "matched",
    "ambiguous",
    "not_found",
    "invalid",
    "conflict",
)
LLM_STATUSES = (
    "not_required",
    "pending",
    "processing",
    "completed",
    "failed",
    "invalid",
    "skipped",
)
CLASSIFICATION_SOURCES = ("deterministic", "llm", "hybrid", "manual")
# Legacy categories kept for backward compat in exports
FINAL_CATEGORIES = (
    "hot_lead",
    "manager_callback",
    "robot_callback",
    "refusal",
    "unknown",
)
PRIMARY_OUTCOMES = (
    "positive",
    "alternate_contact",
    "callback_later",
    "no_answer",
    "refusal",
    "hangup",
    "mixed",
    "manual_review",
    "unsupported_outcome",
)
ACTION_TYPES = (
    "timeline_comment",
    "crm_todo",
    "task",
    "bitrix_add_todo",
    "bitrix_add_comment",
    "bitrix_find_contact",
    "bitrix_create_contact",
    "bitrix_update_contact",
    "bitrix_link_contact_to_deal",
    "retry_queue_add",
    "contact_search_queue_add",
    "manual_review_required",
)
OPERATION_EXECUTION_STATUSES = (
    "prepared",
    "executing",
    "succeeded",
    "failed",
    "skipped",
    "blocked_manual_review",
)
ROW_EXECUTION_STATUSES = (
    "pending",
    "prepared",
    "executing",
    "completed",
    "partial",
    "failed",
    "blocked_manual_review",
)
RETRY_QUEUE_STATUSES = (
    "pending",
    "scheduled",
    "blocked_manual_review",
    "contact_search_required",
    "ready",
    "exported",
    "sent_to_tomoru",
    "completed",
    "cancelled",
    "failed",
)
RETRY_QUEUE_REASONS = (
    "alternate_contact",
    "callback_later",
    "no_answer",
    "hangup_replacement_contact",
)
CONTACT_SEARCH_STATUSES = (
    "contact_search_required",
    "searching",
    "candidate_found",
    "awaiting_confirmation",
    "contact_confirmed",
    "no_contact_found",
    "failed",
    "cancelled",
)
VALIDATION_STATUSES = ("valid", "warning", "invalid")


def empty_business_signals() -> dict[str, Any]:
    return {
        "positive": False,
        "alternate_contact_requested": False,
        "callback_later_requested": False,
        "no_answer": False,
        "deal_not_found": False,
        "explicit_refusal": False,
        "hangup_without_result": False,
        "replacement_contact_required": False,
        "alternate_contact": {
            "name": None,
            "phone": None,
            "extension": None,
            "email": None,
            "position": None,
        },
        "callback_at": None,
        "callback_text": None,
        "summary": "",
        "refusal_reason": None,
        "confidence": 0.0,
        "needs_manual_review": False,
        "manual_review_reason": None,
    }


class CallResultImport(Base):
    __tablename__ = "call_result_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    storage_key: Mapped[str] = mapped_column(String(1024))
    file_sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0)
    selected_sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)
    column_mapping: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True)
    total_rows: Mapped[int] = mapped_column(Integer, default=0)
    matched_rows: Mapped[int] = mapped_column(Integer, default=0)
    review_rows: Mapped[int] = mapped_column(Integer, default=0)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    parser_version: Mapped[str] = mapped_column(String(16), default="1")
    planner_version: Mapped[str] = mapped_column(String(16), default="2")
    classifier_version: Mapped[str] = mapped_column(String(16), default="2")
    duplicate_of_import_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_format: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    batch_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    adapter_version: Mapped[str] = mapped_column(String(16), default="1")
    import_warnings: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    execute_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    execute_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execute_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_rows_total: Mapped[int] = mapped_column(Integer, default=0)
    llm_rows_completed: Mapped[int] = mapped_column(Integer, default=0)
    llm_rows_failed: Mapped[int] = mapped_column(Integer, default=0)
    llm_rows_cached: Mapped[int] = mapped_column(Integer, default=0)
    llm_rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    llm_rows_low_confidence: Mapped[int] = mapped_column(Integer, default=0)
    llm_total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    deterministic_classified: Mapped[int] = mapped_column(Integer, default=0)

    rows: Mapped[list[CallResultImportRow]] = relationship(
        back_populates="import_record", cascade="all, delete-orphan"
    )
    actions: Mapped[list[BitrixPreparedAction]] = relationship(
        back_populates="import_record", cascade="all, delete-orphan"
    )


class CallResultImportRow(Base):
    __tablename__ = "call_result_import_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("call_result_imports.id", ondelete="CASCADE"), index=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    raw_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    normalized_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    raw_phone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    normalized_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    phone_extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    matched_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matched_deal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matched_company_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    matched_deal_local_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_status: Mapped[str] = mapped_column(String(32), default="not_found")
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_matches: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    processing_errors: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    row_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_identity: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    technical_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    call_result_display: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processing_warnings: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    llm_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    merge_conflict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deterministic_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    deterministic_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_required: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_status: Mapped[str] = mapped_column(String(32), default="not_required")
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    llm_schema_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    llm_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_input_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    llm_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_validation_errors: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    llm_error_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_token_usage: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    final_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    classification_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    classification_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    manually_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    manually_overridden_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manually_overridden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # v2 business signals
    business_signals: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    primary_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    needs_manual_review: Mapped[bool] = mapped_column(Boolean, default=False)
    manual_review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_classifier_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    row_planner_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)

    import_record: Mapped[CallResultImport] = relationship(back_populates="rows")
    actions: Mapped[list[BitrixPreparedAction]] = relationship(
        back_populates="import_row", cascade="all, delete-orphan"
    )


class BitrixPreparedAction(Base):
    __tablename__ = "bitrix_prepared_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    import_id: Mapped[int] = mapped_column(ForeignKey("call_result_imports.id", ondelete="CASCADE"), index=True)
    import_row_id: Mapped[int] = mapped_column(
        ForeignKey("call_result_import_rows.id", ondelete="CASCADE"), index=True
    )
    action_group_id: Mapped[str] = mapped_column(String(36))
    method: Mapped[str] = mapped_column(String(64))
    action_type: Mapped[str] = mapped_column(String(32))
    operation_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    human_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_status: Mapped[str] = mapped_column(String(16), default="valid")
    validation_errors: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    user_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    modified_fields: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(32), default="prepared", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("import_id", "idempotency_key", name="uq_bitrix_prepared_actions_idempotency"),
        Index("ix_bpa_row_op_idem", "import_row_id", "operation_type", "idempotency_key"),
    )

    import_record: Mapped[CallResultImport] = relationship(back_populates="actions")
    import_row: Mapped[CallResultImportRow] = relationship(back_populates="actions")


class CallRetryQueueEntry(Base):
    __tablename__ = "call_retry_queue_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    import_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    row_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    phone_normalized: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    callback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    callback_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    source_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    replacement_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    search_required: Mapped[bool] = mapped_column(Boolean, default=False)
    idempotency_key: Mapped[str] = mapped_column(String(512))
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "idempotency_key", name="uq_call_retry_queue_idempotency"),
    )


class CallContactSearchEntry(Base):
    __tablename__ = "call_contact_search_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    import_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    row_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    deal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    region: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deal_contact_ids: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_attempts: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="contact_search_required", index=True)
    found_contact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    found_contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CallResultLlmCache(Base):
    __tablename__ = "call_result_llm_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    input_hash: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(16))
    schema_version: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[dict] = mapped_column(JSONType, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    token_usage: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    use_count: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint("portal_id", "input_hash", name="uq_call_result_llm_cache"),
        Index("ix_call_result_llm_cache_portal_hash", "portal_id", "input_hash"),
    )


class CallResultRowAudit(Base):
    __table_args__ = (Index("ix_call_result_row_audit_row_id", "row_id"),)

    __tablename__ = "call_result_row_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    row_id: Mapped[int] = mapped_column(ForeignKey("call_result_import_rows.id", ondelete="CASCADE"))
    changed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    field_name: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[Any | None] = mapped_column(JSONType, nullable=True)
    new_value: Mapped[Any | None] = mapped_column(JSONType, nullable=True)
