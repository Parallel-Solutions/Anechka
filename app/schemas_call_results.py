"""Pydantic schemas for call result import API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.call_results.llm_schema import AlternateContactData


class MessageResponse(BaseModel):
    message: str
    import_id: int | None = None
    source_format: str | None = None


class DuplicateResponse(BaseModel):
    duplicate: bool = True
    existing_import_id: int
    resumable: bool = False
    message: str


class ImportConfigureRequest(BaseModel):
    selected_sheet: str | None = None
    column_mapping: dict[str, str] | None = None


class ColumnMappingRequest(BaseModel):
    sheet: str | None = None
    column_mapping: dict[str, str] | None = None
    force_duplicate: bool = False


class RowPatchRequest(BaseModel):
    matched_deal_id: int | None = None
    matched_deal_local_id: int | None = None
    final_category: str | None = None
    primary_outcome: str | None = None
    business_signals: dict[str, Any] | None = None
    positive: bool | None = None
    alternate_contact_requested: bool | None = None
    callback_later_requested: bool | None = None
    no_answer: bool | None = None
    deal_not_found: bool | None = None
    explicit_refusal: bool | None = None
    hangup_without_result: bool | None = None
    hangup_during_robocall: bool | None = None
    replacement_contact_required: bool | None = None
    needs_manual_review: bool | None = None
    comment: str | None = None
    summary: str | None = None
    next_action: str | None = None
    callback_at: datetime | None = None
    email: str | None = None
    phone_extension: str | None = None
    full_phone: str | None = None
    contact_name: str | None = None
    responsible_id: int | None = None
    requires_crm_comment: bool | None = None
    requires_crm_todo: bool | None = None
    requires_task: bool | None = None


class ExecuteRequest(BaseModel):
    row_ids: list[int] | None = None
    confirmation_token: str = ""
    retry_failed_only: bool = False


class ExecuteLogItemOut(BaseModel):
    id: int
    import_row_id: int
    source_row_number: int | None = None
    method: str
    human_summary: str | None = None
    execution_status: str
    external_id: str | None = None
    bitrix_external_url: str | None = None
    last_error: str | None = None
    response_payload: dict[str, Any] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ExecuteStatusOut(BaseModel):
    execute_status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    prepared: int = 0
    items: list[ExecuteLogItemOut] | None = None


class ManualPreviewRequest(BaseModel):
    action: Literal["comment", "todo", "find_contact", "create_contact"]


class ManualPreviewOut(BaseModel):
    action: str
    preview_text: str | None = None
    todo_title: str | None = None
    contact_data: AlternateContactData | None = None
    found_contact: dict[str, Any] | None = None
    search_method: str | None = None
    ai_keywords: list[str] | None = None
    error: str | None = None


class ManualResolveRequest(BaseModel):
    action: Literal["comment", "todo", "find_contact", "create_contact"]
    confirmed: bool = True
    preview_text: str | None = None
    todo_title: str | None = None
    todo_description: str | None = None
    contact_data: AlternateContactData | None = None
    found_contact_id: int | None = None
    found_phone: str | None = None


class ManualResolveOut(BaseModel):
    action: str
    message: str
    row_id: int
    prepared_method: str | None = None
    prepared_methods: list[str] = Field(default_factory=list)
    execution_enabled: bool = False
    retry_queue_entry_id: int | None = None
    contact_id: int | None = None
    phone: str | None = None
    lpr_reason: str | None = None


class ActionPatchRequest(BaseModel):
    is_enabled: bool | None = None
    comment_text: str | None = None
    todo_title: str | None = None
    todo_description: str | None = None
    task_title: str | None = None
    task_description: str | None = None
    deadline: datetime | None = None
    reset_to_auto: bool = False


class ActionOut(BaseModel):
    id: int
    import_row_id: int
    source_row_number: int | None = None
    action_group_id: str
    method: str
    action_type: str
    payload: dict[str, Any]
    human_summary: str | None
    validation_status: str
    validation_errors: list[str] | None
    is_enabled: bool
    user_modified: bool
    phone: str | None = None
    deal_title: str | None = None
    bitrix_deal_id: int | None = None
    responsible_name: str | None = None
    task_responsible_user_id: int | None = None
    final_category: str | None = None
    execution_status: str | None = None
    last_error: str | None = None


class RowListOut(BaseModel):
    """Lightweight row for import detail list (no raw_data / llm_result / normalized_data)."""

    id: int
    source_row_number: int
    raw_phone: str | None
    normalized_phone: str | None
    match_status: str
    match_reason: str | None
    final_category: str | None
    classification_source: str | None
    classification_reason: str | None
    llm_status: str
    llm_confidence: float | None
    llm_required: bool
    skip_reason: str | None
    extracted_data: dict | None
    candidate_matches: list | None
    manually_overridden: bool
    deterministic_category: str | None
    deterministic_reason: str | None
    llm_category: str | None = None
    llm_validation_errors: list | None
    matched_deal_id: int | None
    matched_deal_bitrix_url: str | None = None
    matched_deal_local_id: int | None = None
    technical_status: str | None = None
    call_result_display: str | None = None
    attempts: int | None = None
    called_at: datetime | None = None
    callback_at: datetime | None = None
    processing_warnings: list | None = None
    scenario_events: list | None = None
    merge_conflict_reason: str | None = None
    business_signals: dict | None = None
    primary_outcome: str | None = None
    needs_manual_review: bool = False
    manual_review_reason: str | None = None
    execution_status: str | None = None
    ui_disposition: Literal["manual_review", "manual_call", "auto_call"] | None = None
    row_filter: Literal[
        "manual_review",
        "manual_call",
        "auto_call",
        "new_contacts",
        "new_todos",
        "new_comments",
    ] | None = None
    primary_bucket: Literal["manual_review", "auto_call", "new_comments"] | None = None
    dial_phone: str | None = None
    operator_filter: str | None = None
    available_manual_actions: list[str] = Field(default_factory=list)
    manual_review_pending: bool = False


class RowOut(RowListOut):
    llm_result: dict | None = None
    raw_data: dict | None = None
    normalized_data: dict | None = None


class RowRawOut(BaseModel):
    raw_data: dict | None = None


class RowLlmDebugOut(BaseModel):
    system_prompt: str
    user_payload: dict[str, Any]
    user_message: str
    llm_result: dict | None = None
    llm_status: str
    llm_required: bool
    llm_model: str | None = None
    llm_provider: str | None = None
    llm_prompt_version: str | None = None
    llm_schema_version: str | None = None
    llm_input_truncated: bool = False
    llm_input_hash: str | None = None
    rebuilt_input_hash: str | None = None
    input_hash_matches: bool | None = None
    llm_confidence: float | None = None
    llm_validation_errors: list | None = None
    llm_error_type: str | None = None
    llm_duration_ms: int | None = None
    llm_token_usage: dict | None = None
    deterministic_category: str | None = None
    deterministic_reason: str | None = None


class HangupRowOut(BaseModel):
    id: int
    source_row_number: int
    phone: str | None = None
    deal_id: int | None = None
    deal_title: str | None = None
    primary_outcome: str | None = None
    execution_status: str | None = None


class ImportSummaryOut(BaseModel):
    total_rows: int = 0
    unique_phones: int = 0
    total_attempts: int = 0
    exact_duplicates: int = 0
    repeat_phones: int = 0
    meaningful_content_rows: int = 0
    matched_rows: int = 0
    review_rows: int = 0
    skipped_rows: int = 0
    ambiguous_rows: int = 0
    not_found_rows: int = 0
    comments: int = 0
    todos: int = 0
    tasks: int = 0
    disabled_actions: int = 0
    deterministic_classified: int = 0
    llm_sent: int = 0
    llm_completed: int = 0
    llm_pending: int = 0
    llm_failed: int = 0
    llm_failed_config: int = 0
    llm_cached: int = 0
    llm_not_required: int = 0
    low_confidence: int = 0
    manually_overridden: int = 0
    robot_callback: int = 0
    refusal: int = 0
    manual_review: int = 0
    positive: int = 0
    alternate_contact: int = 0
    callback_later: int = 0
    no_answer: int = 0
    pure_no_answer: int = 0
    retry_call_phones: int = 0
    hangup: int = 0
    hangup_during_robocall: int = 0
    hangup_with_answers: int = 0
    hangup_without_answers: int = 0
    prepared_operations: int = 0
    executed_operations: int = 0
    execution_errors: int = 0
    execute_status: str | None = None
    primary_manual_review: int = 0
    primary_auto_call: int = 0
    primary_new_comments: int = 0
    filter_counts: dict[str, int] = Field(default_factory=dict)
    manual_call_inclusive: int = 0


class AttemptHistoryOut(BaseModel):
    normalized_phone: str
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    latest_outcome: str | None = None
    latest_no_answer: bool = False


class ImportStatusOut(BaseModel):
    id: int
    original_filename: str
    campaign_name: str | None = None
    status: str
    error_message: str | None = None
    source_format: str | None = None
    batch_id: str | None = None
    exported_at: datetime | None = None
    created_at: datetime | None = None
    processed_at: datetime | None = None
    summary: ImportSummaryOut


class ImportDetailOut(BaseModel):
    id: int
    original_filename: str
    campaign_name: str | None = None
    status: str
    source_format: str | None = None
    batch_id: str | None = None
    exported_at: datetime | None = None
    import_warnings: list | None = None
    created_at: datetime | None
    processed_at: datetime | None
    error_message: str | None
    duplicate_of_import_id: int | None
    summary: ImportSummaryOut
    rows: list[RowListOut] = Field(default_factory=list)
    actions_by_method: dict[str, list[ActionOut]] = Field(default_factory=dict)
    manual_review_ids: list[int] = Field(default_factory=list)
    attempt_history: list[AttemptHistoryOut] = Field(default_factory=list)
    hangup_rows: list[HangupRowOut] = Field(default_factory=list)
