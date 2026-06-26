"""Pydantic schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class SettingsUpdate(BaseModel):
    bitrix_webhook_url: str = ""
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    connect_timeout: float = Field(default=10.0, gt=0)
    read_timeout: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=5, ge=0, le=20)
    retry_base_delay: float = Field(default=1.0, gt=0)
    max_export_size: int = Field(default=5000, gt=0)
    export_dir: str = "./exports"
    log_level: str = "INFO"

    @field_validator("bitrix_webhook_url")
    @classmethod
    def validate_webhook(cls, v: str) -> str:
        if v and not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError("URL вебхука должен начинаться с http:// или https://")
        return v.strip()


class SettingsResponse(BaseModel):
    bitrix_webhook_url: str
    bitrix_webhook_url_masked: str
    openai_api_key: str
    openai_api_key_masked: str
    openai_model: str
    connect_timeout: float
    read_timeout: float
    max_retries: int
    retry_base_delay: float
    max_export_size: int
    export_dir: str
    log_level: str


class RegionExportRequest(BaseModel):
    region_name: str = Field(min_length=1)
    region_id: int | None = None
    category_id: int = 15
    iblock_id: int = 49
    region_field: str = "UF_CRM_5ECE25C5D78E0"
    limit: int = Field(default=500, gt=0)


class StageExportRequest(BaseModel):
    category_id: int
    stage_id: str = Field(min_length=1)
    limit: int = Field(default=50, gt=0)
    excluded_user_ids: list[int] = Field(default_factory=list)
    excel_format: Literal["normalized", "wide"] = "normalized"
    include_company_phones: bool = True
    include_company_contacts: bool = True
    all_contact_phones: bool = True
    region_field: str = "UF_CRM_5ECE25C5D78E0"


class CategoryFullExportRequest(BaseModel):
    category_id: int
    limit: int = Field(default=5000, gt=0)
    excluded_user_ids: list[int] = Field(default_factory=list)


class RegionSearchResult(BaseModel):
    id: int
    name: str


class CategoryItem(BaseModel):
    id: int
    name: str


class StageItem(BaseModel):
    id: str
    name: str
    category_id: int


class UserItem(BaseModel):
    id: int
    name: str


class ExportJobResponse(BaseModel):
    id: int
    mode: str
    status: str
    progress_current: int
    progress_total: int
    progress_percent: float
    current_step: str
    result_file: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    cancel_requested: bool
    statistics: dict[str, Any]
    event_log: list[str]
    parameters: dict[str, Any]

    model_config = {"from_attributes": True}


class ConnectionTestResponse(BaseModel):
    ok: bool
    message: str


class MessageResponse(BaseModel):
    message: str
    job_id: int | None = None


class AIChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class AIChatRequest(BaseModel):
    messages: list[AIChatMessage] = Field(min_length=1)


class AITableData(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]


class AIChatResponse(BaseModel):
    reply: str
    table: AITableData | None = None
    result_token: str | None = None
    download_url: str | None = None
    download_label: str | None = None


class AIPromptCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)


class AIPromptUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    prompt: str = Field(min_length=1, max_length=4000)


class AIPromptItem(BaseModel):
    id: int
    title: str
    prompt: str
    sort_order: int

    model_config = {"from_attributes": True}


class LprConfigData(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    stopwords: list[str] = Field(default_factory=list)
