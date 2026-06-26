"""Конфигурация приложения: SQLite (UI) → env → defaults."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SETTING_KEYS = (
    "bitrix_webhook_url",
    "openai_api_key",
    "openai_model",
    "openai_bitrix_metadata_model",
    "connect_timeout",
    "read_timeout",
    "max_retries",
    "retry_base_delay",
    "max_export_size",
    "export_dir",
    "file_storage_dir",
    "log_level",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_secret_key: str = Field(default="change-me", alias="APP_SECRET_KEY")
    database_url: str = Field(
        default="postgresql+psycopg://bitrix:bitrix@db:5432/bitrix_export",
        alias="DATABASE_URL",
    )
    bitrix_webhook_url: str = Field(default="", alias="BITRIX_WEBHOOK_URL")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o", alias="OPENAI_MODEL")
    openai_bitrix_metadata_model: str = Field(default="", alias="OPENAI_BITRIX_METADATA_MODEL")
    connect_timeout: float = Field(default=10.0, alias="CONNECT_TIMEOUT")
    read_timeout: float = Field(default=60.0, alias="READ_TIMEOUT")
    max_retries: int = Field(default=5, alias="MAX_RETRIES")
    retry_base_delay: float = Field(default=1.0, alias="RETRY_BASE_DELAY")
    max_export_size: int = Field(default=5000, alias="MAX_EXPORT_SIZE")
    export_dir: str = Field(default=str(BASE_DIR / "exports"), alias="EXPORT_DIR")
    file_storage_dir: str = Field(default=str(BASE_DIR / "filestorage"), alias="FILE_STORAGE_DIR")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    max_workers: int = Field(default=2, alias="MAX_WORKERS")
    worker_poll_interval: float = Field(default=2.0, alias="WORKER_POLL_INTERVAL")
    worker_heartbeat_interval: float = Field(default=30.0, alias="WORKER_HEARTBEAT_INTERVAL")
    worker_stale_run_minutes: int = Field(default=15, alias="WORKER_STALE_RUN_MINUTES")
    import_batch_size: int = Field(default=50, alias="IMPORT_BATCH_SIZE")
    import_overlap_minutes: int = Field(default=10, alias="IMPORT_OVERLAP_MINUTES")
    bitrix_metadata_prompt_version: str = Field(default="1", alias="BITRIX_METADATA_PROMPT_VERSION")

    # --- HTTP Basic Auth (empty password = disabled) ---
    basic_auth_username: str = Field(default="admin", alias="BASIC_AUTH_USERNAME")
    basic_auth_password: str = Field(default="", alias="BASIC_AUTH_PASSWORD")

    # --- Auth / sessions ---
    bootstrap_admin_email: str = Field(default="", alias="BOOTSTRAP_ADMIN_EMAIL")
    bootstrap_admin_password: str = Field(default="", alias="BOOTSTRAP_ADMIN_PASSWORD")
    session_cookie_name: str = Field(default="ie_session", alias="SESSION_COOKIE_NAME")
    session_max_age_seconds: int = Field(default=60 * 60 * 12, alias="SESSION_MAX_AGE_SECONDS")
    cookie_secure: bool = Field(default=False, alias="COOKIE_SECURE")

    # --- Intelligent export ---
    ie_default_portal_id: str = Field(default="default", alias="IE_DEFAULT_PORTAL_ID")
    ie_preview_rows: int = Field(default=100, alias="IE_PREVIEW_ROWS")
    ie_max_export_rows: int = Field(default=100000, alias="IE_MAX_EXPORT_ROWS")
    ie_staleness_warn_hours: int = Field(default=24, alias="IE_STALENESS_WARN_HOURS")
    ie_staleness_block_hours: int = Field(default=72, alias="IE_STALENESS_BLOCK_HOURS")
    ie_statement_timeout_ms: int = Field(default=30000, alias="IE_STATEMENT_TIMEOUT_MS")
    ie_max_message_chars: int = Field(default=8000, alias="IE_MAX_MESSAGE_CHARS")
    ie_max_history_messages: int = Field(default=20, alias="IE_MAX_HISTORY_MESSAGES")
    # --- Planner (export plan generation) ---
    ie_planner_model: str = Field(default="", alias="IE_PLANNER_MODEL")
    ie_planner_temperature: float = Field(default=0.0, alias="IE_PLANNER_TEMPERATURE")
    # Keep the self-repair loop short so a failing turn fails fast (~15s) with
    # actionable fix suggestions instead of spinning through many slow retries.
    ie_planner_max_repair_attempts: int = Field(default=2, alias="IE_PLANNER_MAX_REPAIR_ATTEMPTS")
    # Per-call timeout (seconds) for the OpenAI planner request.
    ie_planner_timeout_seconds: float = Field(default=30.0, alias="IE_PLANNER_TIMEOUT_SECONDS")
    ie_catalog_field_budget: int = Field(default=120, alias="IE_CATALOG_FIELD_BUDGET")
    # --- DB-aware memory generator (profiler thresholds) ---
    ie_profile_sample_cap: int = Field(default=5000, alias="IE_PROFILE_SAMPLE_CAP")
    ie_profile_low_fill_threshold: float = Field(default=0.10, alias="IE_PROFILE_LOW_FILL_THRESHOLD")
    ie_profile_link_threshold: float = Field(default=0.30, alias="IE_PROFILE_LINK_THRESHOLD")
    ie_profile_min_rows: int = Field(default=20, alias="IE_PROFILE_MIN_ROWS")
    ie_memory_generate_max: int = Field(default=20, alias="IE_MEMORY_GENERATE_MAX")
    ie_run_retention_days: int = Field(default=90, alias="IE_RUN_RETENTION_DAYS")
    ie_audit_retention_days: int = Field(default=365, alias="IE_AUDIT_RETENTION_DAYS")


def _env_overrides() -> dict[str, Any]:
    mapping = {
        "bitrix_webhook_url": "BITRIX_WEBHOOK_URL",
        "openai_api_key": "OPENAI_API_KEY",
        "openai_model": "OPENAI_MODEL",
        "openai_bitrix_metadata_model": "OPENAI_BITRIX_METADATA_MODEL",
        "connect_timeout": "CONNECT_TIMEOUT",
        "read_timeout": "READ_TIMEOUT",
        "max_retries": "MAX_RETRIES",
        "retry_base_delay": "RETRY_BASE_DELAY",
        "max_export_size": "MAX_EXPORT_SIZE",
        "export_dir": "EXPORT_DIR",
        "file_storage_dir": "FILE_STORAGE_DIR",
        "log_level": "LOG_LEVEL",
    }
    overrides: dict[str, Any] = {}
    for key, env_name in mapping.items():
        val = os.environ.get(env_name)
        if val is not None and val != "":
            if key in ("connect_timeout", "read_timeout", "retry_base_delay"):
                overrides[key] = float(val)
            elif key in ("max_retries", "max_export_size"):
                overrides[key] = int(val)
            else:
                overrides[key] = val
    return overrides


def get_file_storage_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    path = Path(s.file_storage_dir)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def get_metadata_model(settings: Settings) -> str:
    return settings.openai_bitrix_metadata_model or settings.openai_model


def get_planner_model(settings: Settings) -> str:
    """Model used for export-plan generation.

    Prefers the dedicated planner model, then the general chat model. The
    metadata model is intentionally NOT used here: planning benefits from the
    strongest available model.
    """
    return settings.ie_planner_model or settings.openai_model


def get_export_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    path = Path(s.export_dir)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings(**_env_overrides())


def merge_db_settings(db_values: dict[str, str]) -> Settings:
    """Объединяет настройки: db > env > defaults."""
    base = get_settings().model_dump()
    env = _env_overrides()
    merged = {**base, **env}
    for key, raw in db_values.items():
        if key in ("connect_timeout", "read_timeout", "retry_base_delay"):
            merged[key] = float(raw)
        elif key in ("max_retries", "max_export_size"):
            merged[key] = int(raw)
        else:
            merged[key] = raw
    # model_construct не перечитывает env и сохраняет приоритет db_values
    return Settings.model_construct(**merged)


def settings_to_db_dict(settings: Settings) -> dict[str, str]:
    return {
        "bitrix_webhook_url": settings.bitrix_webhook_url,
        "openai_api_key": settings.openai_api_key,
        "openai_model": settings.openai_model,
        "openai_bitrix_metadata_model": settings.openai_bitrix_metadata_model,
        "connect_timeout": str(settings.connect_timeout),
        "read_timeout": str(settings.read_timeout),
        "max_retries": str(settings.max_retries),
        "retry_base_delay": str(settings.retry_base_delay),
        "max_export_size": str(settings.max_export_size),
        "export_dir": settings.export_dir,
        "file_storage_dir": settings.file_storage_dir,
        "log_level": settings.log_level,
    }
