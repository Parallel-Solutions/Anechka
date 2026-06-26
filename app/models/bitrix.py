"""SQLAlchemy models — Bitrix CRM import."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.db.types import JSONType
from app.models.legacy import utcnow

# Entity type IDs
ENTITY_LEAD = 1
ENTITY_DEAL = 2
ENTITY_CONTACT = 3
ENTITY_COMPANY = 4

SYNC_MODES = ("full", "incremental", "reconciliation", "schema_only", "ai_reanalysis", "contacts_backfill")
SYNC_STATUSES = ("pending", "running", "completed", "failed", "cancelled")


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255), index=True)
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    current_phase: Mapped[str] = mapped_column(String(128), default="")
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    api_requests_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_requests_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    statistics_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    resource_name: Mapped[str] = mapped_column(String(64))
    entity_type_id: Mapped[int] = mapped_column(Integer, default=0)
    cursor_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cursor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconciliation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "resource_name", "entity_type_id", name="uq_sync_checkpoint"),
    )


class CrmEntity(Base):
    __tablename__ = "crm_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    entity_type_id: Mapped[int] = mapped_column(Integer)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    entity_kind: Mapped[str] = mapped_column(String(32), default="")
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assigned_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    currency_id: Mapped[str | None] = mapped_column(String(8), nullable=True)
    amount: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), index=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "entity_type_id", "entity_id", name="uq_crm_entity"),
        Index("ix_crm_entities_portal_type_updated", "portal_id", "entity_type_id", "updated_time", "entity_id"),
    )


class CrmEntityVersion(Base):
    __tablename__ = "crm_entity_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    entity_type_id: Mapped[int] = mapped_column(Integer)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    payload_hash: Mapped[str] = mapped_column(String(64))
    raw_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    change_source: Mapped[str] = mapped_column(String(32), default="import")
    sync_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CrmFieldDefinition(Base):
    __tablename__ = "crm_field_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    entity_type_id: Mapped[int] = mapped_column(Integer)
    original_field_name: Mapped[str] = mapped_column(String(255))
    api_field_name: Mapped[str] = mapped_column(String(255), default="")
    upper_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    list_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    form_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    filter_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    field_type: Mapped[str] = mapped_column(String(64), default="")
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    is_multiple: Mapped[bool] = mapped_column(Boolean, default=False)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_read_only: Mapped[bool] = mapped_column(Boolean, default=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, default=False)
    settings: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    raw_definition: Mapped[dict] = mapped_column(JSONType, default=dict)
    definition_hash: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    discovered_from_payload: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "entity_type_id", "original_field_name", name="uq_crm_field_def"),
    )


class CrmFieldDefinitionVersion(Base):
    __tablename__ = "crm_field_definition_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    field_definition_id: Mapped[int] = mapped_column(Integer, index=True)
    definition_hash: Mapped[str] = mapped_column(String(64))
    raw_definition: Mapped[dict] = mapped_column(JSONType, default=dict)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CrmFieldSemantic(Base):
    __tablename__ = "crm_field_semantics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    field_definition_id: Mapped[int] = mapped_column(Integer, index=True)
    language: Mapped[str] = mapped_column(String(8), default="ru")
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detailed_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_purpose: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_data_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_dictionary: Mapped[bool] = mapped_column(Boolean, default=False)
    dictionary_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    nullable_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True)
    warnings: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    source_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    prompt_version: Mapped[str] = mapped_column(String(16), default="1")
    model_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)


class CrmDictionary(Base):
    __tablename__ = "crm_dictionaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    entity_type_id: Mapped[int] = mapped_column(Integer, default=0)
    field_definition_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dictionary_code: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    short_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detailed_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    generated_by_ai: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CrmDictionaryEntry(Base):
    __tablename__ = "crm_dictionary_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dictionary_id: Mapped[int] = mapped_column(Integer, index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    xml_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    semantic_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    source_hash: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("dictionary_id", "external_id", name="uq_dict_entry"),
    )


class CrmEntityFieldValue(Base):
    __tablename__ = "crm_entity_field_values"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    entity_type_id: Mapped[int] = mapped_column(Integer)
    entity_id: Mapped[int] = mapped_column(BigInteger)
    field_definition_id: Mapped[int] = mapped_column(Integer, index=True)
    value_index: Mapped[int] = mapped_column(Integer, default=0)
    raw_value: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    text_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    numeric_value: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    boolean_value: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    date_value: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    datetime_value: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dictionary_entry_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_entity_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    related_entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    value_hash: Mapped[str] = mapped_column(String(64), default="")
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)


class CrmChildRecord(Base):
    """Generic related/child data table."""

    __tablename__ = "crm_child_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    record_type: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(255))
    parent_entity_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "record_type", "external_id", name="uq_crm_child"),
        Index("ix_crm_child_parent", "portal_id", "parent_entity_type_id", "parent_entity_id"),
    )


class CrmFile(Base):
    __tablename__ = "crm_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    bitrix_file_id: Mapped[str] = mapped_column(String(64))
    disk_object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_entity_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parent_entity_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    field_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    download_status: Mapped[str] = mapped_column(String(32), default="pending")
    raw_metadata: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "bitrix_file_id", name="uq_crm_file"),
    )


class CrmUser(Base):
    __tablename__ = "crm_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[int] = mapped_column(Integer)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "external_id", name="uq_crm_user"),
    )


class CrmCurrency(Base):
    __tablename__ = "crm_currencies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    currency_code: Mapped[str] = mapped_column(String(8))
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "currency_code", name="uq_crm_currency"),
    )


class CrmFieldValueProfile(Base):
    """Накопленный профиль значений одного поля (включая поля вне crm.item.fields)."""

    __tablename__ = "crm_field_value_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    entity_type_id: Mapped[int] = mapped_column(Integer)
    field_code: Mapped[str] = mapped_column(String(255))
    field_definition_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    observed_types: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    filled_count: Mapped[int] = mapped_column(Integer, default=0)
    null_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_values: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    numeric_stats: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    length_stats: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    value_signature: Mapped[str] = mapped_column(String(64), default="", index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "entity_type_id", "field_code", name="uq_crm_field_value_profile"),
    )


class CrmContact(Base):
    __tablename__ = "crm_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    contact_id: Mapped[int] = mapped_column(BigInteger)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    source_lead_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    second_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    post: Mapped[str | None] = mapped_column(String(512), nullable=True)
    post_custom: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    company_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    primary_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    primary_phone_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    first_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "contact_id", name="uq_crm_contact"),
        Index("ix_crm_contacts_company", "portal_id", "company_id"),
    )


class CrmContactPhone(Base):
    __tablename__ = "crm_contact_phones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    contact_id: Mapped[int] = mapped_column(BigInteger)
    value: Mapped[str] = mapped_column(String(64))
    value_type: Mapped[str] = mapped_column(String(16), default="OTHER")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    first_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint("portal_id", "contact_id", "value", name="uq_crm_contact_phone"),
        Index("ix_crm_contact_phones_contact", "portal_id", "contact_id"),
    )


class CrmContactLink(Base):
    __tablename__ = "crm_contact_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portal_id: Mapped[str] = mapped_column(String(255))
    contact_id: Mapped[int] = mapped_column(BigInteger)
    parent_entity_type_id: Mapped[int] = mapped_column(Integer)
    parent_entity_id: Mapped[int] = mapped_column(BigInteger)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    first_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "portal_id",
            "contact_id",
            "parent_entity_type_id",
            "parent_entity_id",
            name="uq_crm_contact_link",
        ),
        Index("ix_crm_contact_links_parent", "portal_id", "parent_entity_type_id", "parent_entity_id"),
    )
