"""FastAPI application entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError

from app.config import BASE_DIR, get_export_dir, get_settings
from app.database import SessionLocal, engine
from app.dependencies import get_app_settings
from app.logging_config import setup_logging
from app.routers import admin_bitrix, ai, bitrix, call_results, exports, intelligent_export, pages, settings
from app.services.job_service import JobService
from app.services.ai_prompt_service import AiPromptService

logger = logging.getLogger(__name__)


def _log_startup_diagnostics(db, app_settings) -> None:
    """Log which database we are actually connected to and whether it has data.

    This makes a wrong-DB situation (e.g. a stray host instance pointing at an
    empty database) immediately obvious in the logs instead of surfacing later
    as mysterious "nothing to export" errors.
    """
    from app.models.bitrix import CrmEntity, CrmFieldDefinition
    from app.services.auth_service import resolve_portal_id

    url = engine.url
    target = f"{url.host or 'local'}:{url.port or '-'}/{url.database}"
    try:
        portal_id = resolve_portal_id(app_settings)
        crm_count = db.scalar(
            select(func.count()).select_from(CrmEntity).where(CrmEntity.portal_id == portal_id)
        ) or 0
        field_count = db.scalar(
            select(func.count())
            .select_from(CrmFieldDefinition)
            .where(CrmFieldDefinition.portal_id == portal_id)
        ) or 0
        logger.info(
            "Startup DB check: db=%s portal=%s crm_entities=%s field_definitions=%s",
            target,
            portal_id,
            crm_count,
            field_count,
        )
        if crm_count == 0:
            logger.warning(
                "Startup DB check: 0 CRM entities for portal %s at %s — экспорт работать не будет, "
                "пока не выполнен импорт данных. Убедитесь, что приложение подключено к нужной БД.",
                portal_id,
                target,
            )
        try:
            from app.models.call_results import CallResultImport

            db.scalar(select(func.count()).select_from(CallResultImport))
        except ProgrammingError:
            db.rollback()
            logger.warning(
                "Startup DB check: call_result_imports table missing at %s — "
                "/call-results will return 500. Run: docker compose run --rm migrate alembic upgrade head",
                target,
            )
    except Exception:
        logger.exception("Startup DB check failed for %s", target)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level)
    get_export_dir(settings)
    from app.config import get_file_storage_dir

    get_file_storage_dir(settings)
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    db = SessionLocal()
    try:
        JobService().recover_interrupted_jobs(db)
        AiPromptService().ensure_defaults(db)
        from app.services.auth_service import AuthService

        app_settings = get_app_settings(db)
        auth = AuthService(app_settings, db)
        auth.ensure_bootstrap_admin()
        auth.ensure_default_ie_user()
        _log_startup_diagnostics(db, app_settings)
    finally:
        db.close()
    yield
    JobService.shutdown()
    from app.services.call_results.job_service import CallResultJobService
    CallResultJobService.shutdown()


app = FastAPI(
    title="Bitrix24 Export",
    description="Выгрузка сделок Bitrix24 в Excel",
    version="1.0.0",
    lifespan=lifespan,
)

static_dir = BASE_DIR / "app" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(pages.router)
app.include_router(settings.router)
app.include_router(bitrix.router)
app.include_router(exports.router)
app.include_router(ai.router)
app.include_router(admin_bitrix.router)
app.include_router(intelligent_export.router)
app.include_router(call_results.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.middleware("http")
async def enforce_basic_auth(request, call_next):
    from app.config import get_settings
    from app.middleware.basic_auth import basic_auth_required

    settings = get_settings()
    if not settings.basic_auth_password:
        return await call_next(request)
    denied = basic_auth_required(request, settings.basic_auth_username, settings.basic_auth_password)
    if denied is not None:
        return denied
    return await call_next(request)
