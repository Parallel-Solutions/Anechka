"""Pydantic schemas for Bitrix import admin API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


SyncMode = Literal["full", "incremental", "reconciliation", "schema_only", "ai_reanalysis", "contacts_backfill"]


class ImportCreateRequest(BaseModel):
    mode: SyncMode = "incremental"
    analyze_metadata: bool = True
    confirm_full: bool = False


class ImportResponse(BaseModel):
    id: int
    portal_id: str
    mode: str
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None
    requested_by: str | None
    current_phase: str
    processed_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    deleted_count: int
    failed_count: int
    api_requests_count: int
    ai_requests_count: int
    last_error: str | None
    statistics: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ImportListResponse(BaseModel):
    items: list[ImportResponse]
    total: int


class EntityListResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class EntityDetailResponse(BaseModel):
    entity: dict[str, Any]
    versions: list[dict[str, Any]]
    child_records: list[dict[str, Any]]
    field_values: list[dict[str, Any]]


class FieldResponse(BaseModel):
    id: int
    entity_type_id: int
    original_field_name: str
    title: str | None
    field_type: str
    is_custom: bool
    is_multiple: bool
    is_required: bool
    is_active: bool
    definition_hash: str
    last_seen_at: datetime
    semantic: dict[str, Any] | None = None

    model_config = {"from_attributes": True}


class FieldSemanticPatch(BaseModel):
    display_name: str | None = None
    short_description: str | None = None
    detailed_description: str | None = None
    business_purpose: str | None = None
    reviewed_by: str = "admin"


class DictionaryResponse(BaseModel):
    id: int
    dictionary_code: str
    title: str | None
    source_type: str
    entity_type_id: int
    field_definition_id: int | None
    is_active: bool
    entries_count: int = 0

    model_config = {"from_attributes": True}


class DictionaryEntryPatch(BaseModel):
    normalized_value: str | None = None
    description: str | None = None
    reviewed_by: str = "admin"


class DashboardResponse(BaseModel):
    bitrix_connected: bool
    portal_id: str
    last_full_import: datetime | None
    last_incremental_import: datetime | None
    checkpoint: dict[str, Any] | None
    leads_count: int
    deals_count: int
    deleted_count: int
    fields_count: int
    custom_fields_count: int
    dictionaries_count: int
    fields_needing_review: int
    worker_active: bool
    last_sync_run: ImportResponse | None
    last_error: str | None
    schedule_enabled: bool = False
    schedule_interval_minutes: int = 60


class MessageResponse(BaseModel):
    message: str
    import_id: int | None = None
