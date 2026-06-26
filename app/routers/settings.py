"""API и форма настроек."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR, get_export_dir, merge_db_settings
from app.database import get_db
from app.dependencies import get_app_settings
from app.exceptions import BitrixAuthenticationError
from app.logging_config import setup_logging
from app.schemas import ConnectionTestResponse, SettingsResponse, SettingsUpdate
from app.services.bitrix_client import BitrixClient
from app.services.security_service import mask_secret, mask_webhook
from app.services.settings_service import load_settings_from_db, save_settings_to_db

router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _to_response(settings) -> SettingsResponse:
    return SettingsResponse(
        bitrix_webhook_url=settings.bitrix_webhook_url,
        bitrix_webhook_url_masked=mask_webhook(settings.bitrix_webhook_url),
        openai_api_key=settings.openai_api_key,
        openai_api_key_masked=mask_secret(settings.openai_api_key),
        openai_model=settings.openai_model,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        max_retries=settings.max_retries,
        retry_base_delay=settings.retry_base_delay,
        max_export_size=settings.max_export_size,
        export_dir=settings.export_dir,
        log_level=settings.log_level,
    )


@router.post("/settings")
def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    bitrix_webhook_url: str = Form(""),
    openai_api_key: str = Form(""),
    openai_model: str = Form("gpt-4o"),
    connect_timeout: float = Form(10.0),
    read_timeout: float = Form(60.0),
    max_retries: int = Form(5),
    retry_base_delay: float = Form(1.0),
    max_export_size: int = Form(5000),
    export_dir: str = Form("./exports"),
    log_level: str = Form("INFO"),
):
    current = get_app_settings(db)
    webhook_before = current.bitrix_webhook_url
    webhook = bitrix_webhook_url.strip()
    if not webhook or webhook == mask_webhook(current.bitrix_webhook_url):
        webhook = current.bitrix_webhook_url

    api_key = openai_api_key.strip()
    if not api_key or api_key == mask_secret(current.openai_api_key):
        api_key = current.openai_api_key

    update = SettingsUpdate(
        bitrix_webhook_url=webhook,
        openai_api_key=api_key,
        openai_model=openai_model.strip() or "gpt-4o",
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        max_export_size=max_export_size,
        export_dir=export_dir,
        log_level=log_level,
    )
    db_values = load_settings_from_db(db)
    db_values.update({k: str(v) for k, v in update.model_dump().items()})
    save_settings_to_db(db, db_values)
    setup_logging(log_level)
    try:
        get_export_dir(merge_db_settings(load_settings_from_db(db)))
    except OSError:
        pass
    webhook_status = "updated" if webhook != webhook_before else "unchanged"
    return RedirectResponse(url=f"/settings?saved=1&webhook={webhook_status}", status_code=303)


@router.post("/api/connection/test", response_model=ConnectionTestResponse)
def test_connection(db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    if not settings.bitrix_webhook_url:
        return ConnectionTestResponse(ok=False, message="URL вебхука не настроен")
    try:
        BitrixClient(settings).test_connection()
        return ConnectionTestResponse(ok=True, message="Подключение к Bitrix24 успешно")
    except BitrixAuthenticationError:
        return ConnectionTestResponse(
            ok=False,
            message="Не удалось подключиться к Bitrix24. Проверьте вебхук",
        )
    except Exception:
        return ConnectionTestResponse(ok=False, message="Ошибка при проверке подключения")
