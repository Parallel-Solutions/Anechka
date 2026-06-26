"""AI chat endpoints."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR, get_export_dir
from app.database import get_db
from app.dependencies import get_app_settings
from app.exceptions import AppError
from app.schemas import (
    AIChatRequest,
    AIChatResponse,
    AIPromptCreate,
    AIPromptItem,
    AIPromptUpdate,
    AITableData,
    LprConfigData,
)
from app.services.ai_prompt_service import AiPromptService
from app.services.ai_service import AIService
from app.services.excel_service import ExcelService
from app.services.lpr_service import load_lpr_config, save_lpr_config
from app.services.security_service import mask_secret, safe_filename, unique_filepath, validate_download_path

router = APIRouter(tags=["ai"])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _ai_cache_dir(settings) -> Path:
    path = get_export_dir(settings) / "ai"
    path.mkdir(parents=True, exist_ok=True)
    return path


@router.get("/ai", response_class=HTMLResponse)
def ai_page(request: Request, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    return templates.TemplateResponse(
        request,
        "ai.html",
        {
            "openai_configured": bool(settings.openai_api_key),
            "bitrix_configured": bool(settings.bitrix_webhook_url),
            "openai_key_masked": mask_secret(settings.openai_api_key),
        },
    )


@router.post("/api/ai/chat", response_model=AIChatResponse)
def ai_chat(body: AIChatRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    if not settings.openai_api_key:
        raise HTTPException(status_code=400, detail="OpenAI API ключ не настроен")

    messages = [{"role": m.role, "content": m.content} for m in body.messages]
    try:
        result = AIService(settings, db).chat(messages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Ошибка OpenAI: {exc}") from exc

    table: AITableData | None = None
    result_token: str | None = None
    if result.table_rows:
        table = AITableData(columns=result.table_columns, rows=result.table_rows)
        # Для ЛПР/Tomoru используется готовый двухлистовой файл (download_url),
        # поэтому одностраничный generic-файл по токену не формируем.
        if not result.download_url:
            result_token = uuid.uuid4().hex
            cache_path = _ai_cache_dir(settings) / f"{result_token}.json"
            cache_path.write_text(
                json.dumps(
                    {"columns": result.table_columns, "rows": result.table_rows},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    return AIChatResponse(
        reply=result.reply,
        table=table,
        result_token=result_token,
        download_url=result.download_url,
        download_label=result.download_label,
    )


@router.get("/api/ai/prompts", response_model=list[AIPromptItem])
def list_ai_prompts(db: Session = Depends(get_db)):
    AiPromptService().ensure_defaults(db)
    return AiPromptService().list_prompts(db)


@router.post("/api/ai/prompts", response_model=AIPromptItem)
def create_ai_prompt(body: AIPromptCreate, db: Session = Depends(get_db)):
    try:
        return AiPromptService().create_prompt(db, body.title, body.prompt)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/ai/prompts/{prompt_id}", response_model=AIPromptItem)
def update_ai_prompt(prompt_id: int, body: AIPromptUpdate, db: Session = Depends(get_db)):
    try:
        return AiPromptService().update_prompt(db, prompt_id, body.title, body.prompt)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/ai/prompts/{prompt_id}")
def delete_ai_prompt(prompt_id: int, db: Session = Depends(get_db)):
    try:
        AiPromptService().delete_prompt(db, prompt_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"message": "Промпт удалён"}


@router.get("/api/ai/lpr-config", response_model=LprConfigData)
def get_lpr_config(db: Session = Depends(get_db)):
    config = load_lpr_config(db)
    return LprConfigData(
        keywords=config.keywords,
        fields=config.fields,
        stopwords=config.stopwords,
    )


@router.put("/api/ai/lpr-config", response_model=LprConfigData)
def update_lpr_config(body: LprConfigData, db: Session = Depends(get_db)):
    config = save_lpr_config(db, body.keywords, body.fields, body.stopwords)
    return LprConfigData(
        keywords=config.keywords,
        fields=config.fields,
        stopwords=config.stopwords,
    )


@router.get("/api/ai/result/{token}/download")
def download_ai_result(token: str, db: Session = Depends(get_db)):
    if not token.isalnum() or len(token) != 32:
        raise HTTPException(status_code=400, detail="Некорректный токен")

    settings = get_app_settings(db)
    cache_dir = _ai_cache_dir(settings)
    cache_path = cache_dir / f"{token}.json"
    if not cache_path.is_file():
        raise HTTPException(status_code=404, detail="Результат не найден или устарел")

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="Повреждённые данные результата") from exc

    rows = payload.get("rows") or []
    filename = safe_filename("ai_chat", token[:8])
    filepath = unique_filepath(cache_dir, filename)
    ExcelService().build_generic(rows, filepath)

    try:
        path = validate_download_path(cache_dir, str(filepath))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/api/ai/result/{token}/download/json")
def download_ai_result_json(token: str, db: Session = Depends(get_db)):
    if not token.isalnum() or len(token) != 32:
        raise HTTPException(status_code=400, detail="Некорректный токен")

    settings = get_app_settings(db)
    cache_dir = _ai_cache_dir(settings)
    cache_path = cache_dir / f"{token}.json"
    if not cache_path.is_file():
        raise HTTPException(status_code=404, detail="Результат не найден или устарел")

    try:
        path = validate_download_path(cache_dir, str(cache_path))
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return FileResponse(
        path,
        filename=cache_path.name,
        media_type="application/json",
    )
