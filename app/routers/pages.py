"""HTML-страницы."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import BASE_DIR, _env_overrides
from app.database import get_db
from app.dependencies import get_app_settings
from app.models import ExportJob
from app.repositories.contact_repository import ContactRepository
from app.repositories.crm_repository import CrmRepository
from app.repositories.sync_repository import SyncRepository
from app.schemas_bitrix import DashboardResponse
from app.services.bitrix_client import BitrixClient
from app.services.intelligent_export.staleness import compute_sync_state
from app.services.lpr_service import load_lpr_config
from app.services.security_service import format_local_dt, mask_secret, mask_webhook
from app.utils.portal import portal_id_from_webhook

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


def _has_json_download(job: ExportJob | None) -> bool:
    if not job or job.status != "completed" or not job.result_file:
        return False
    return Path(job.result_file).is_file()


def _intelligent_export_page(request: Request):
    return templates.TemplateResponse(request, "intelligent_export.html", {})


@router.get("/intelligent-export", response_class=HTMLResponse)
def intelligent_export_page(request: Request):
    return _intelligent_export_page(request)


def _tomoru_export_page(request: Request, db: Session):
    settings = get_app_settings(db)
    jobs = (
        db.query(ExportJob)
        .filter(ExportJob.mode == "region_lpr")
        .order_by(ExportJob.created_at.desc())
        .limit(20)
        .all()
    )
    bitrix_ok = False
    if settings.bitrix_webhook_url:
        try:
            bitrix_ok = BitrixClient(settings).test_connection()
        except Exception:
            bitrix_ok = False
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    sync_state = compute_sync_state(db, portal, settings)
    return templates.TemplateResponse(
        request,
        "tomoru_export.html",
        {
            "jobs": jobs,
            "bitrix_connected": bitrix_ok,
            "format_dt": format_local_dt,
            "sync_state": sync_state.to_dict(),
        },
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    return _tomoru_export_page(request, db)


@router.get("/tomoru-export", response_class=HTMLResponse)
def tomoru_export_page(request: Request, db: Session = Depends(get_db)):
    return _tomoru_export_page(request, db)


@router.get("/legacy-export", response_class=HTMLResponse)
def legacy_export_page(request: Request, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    jobs = db.query(ExportJob).order_by(ExportJob.created_at.desc()).limit(20).all()
    bitrix_ok = False
    if settings.bitrix_webhook_url:
        try:
            bitrix_ok = BitrixClient(settings).test_connection()
        except Exception:
            bitrix_ok = False
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "bitrix_connected": bitrix_ok,
            "webhook_masked": mask_webhook(settings.bitrix_webhook_url),
            "max_export_size": settings.max_export_size,
            "format_dt": format_local_dt,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    lpr_config = load_lpr_config(db)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "settings": settings,
            "webhook_masked": mask_webhook(settings.bitrix_webhook_url),
            "openai_key_masked": mask_secret(settings.openai_api_key),
            "env_webhook_set": "bitrix_webhook_url" in _env_overrides(),
            "lpr_config": lpr_config,
        },
    )


@router.get("/exports", response_class=HTMLResponse)
def exports_list(request: Request, db: Session = Depends(get_db)):
    jobs = db.query(ExportJob).order_by(ExportJob.created_at.desc()).all()
    return templates.TemplateResponse(
        request,
        "exports.html",
        {"jobs": jobs, "format_dt": format_local_dt, "has_json_download": _has_json_download},
    )


ENTITY_LABELS = {1: "Лиды", 2: "Сделки", 3: "Контакты", 4: "Компании"}


@router.get("/bitrix-import", response_class=HTMLResponse)
def bitrix_import_dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    sync_repo = SyncRepository(db)
    crm_repo = CrmRepository(db, portal)
    bitrix_ok = False
    if settings.bitrix_webhook_url:
        try:
            bitrix_ok = BitrixClient(settings).test_connection()
        except Exception:
            pass
    last_full = sync_repo.last_successful_run(portal, "full")
    last_incr = sync_repo.last_successful_run(portal, "incremental")
    cp = sync_repo.get_checkpoint(portal, "entities", 1)
    from app.models import SyncRun
    from sqlalchemy import func, select
    from app.models import CrmDictionary, CrmFieldDefinition, CrmFieldSemantic

    last_run = db.scalar(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(1))
    fields_needing = db.scalar(
        select(func.count())
        .select_from(CrmFieldSemantic)
        .join(CrmFieldDefinition, CrmFieldDefinition.id == CrmFieldSemantic.field_definition_id)
        .where(CrmFieldDefinition.portal_id == portal, CrmFieldSemantic.needs_review.is_(True))
    ) or 0
    dict_count = db.scalar(
        select(func.count()).select_from(CrmDictionary).where(CrmDictionary.portal_id == portal)
    ) or 0

    dashboard = DashboardResponse(
        bitrix_connected=bitrix_ok,
        portal_id=portal,
        last_full_import=last_full.finished_at if last_full else None,
        last_incremental_import=last_incr.finished_at if last_incr else None,
        checkpoint={"cursor_time": cp.cursor_time.isoformat() if cp and cp.cursor_time else None, "cursor_id": cp.cursor_id if cp else None} if cp else None,
        leads_count=crm_repo.count_entities(1, is_deleted=False),
        deals_count=crm_repo.count_entities(2, is_deleted=False),
        deleted_count=crm_repo.count_entities(is_deleted=True),
        fields_count=crm_repo.count_fields(),
        custom_fields_count=crm_repo.count_fields(custom_only=True),
        dictionaries_count=dict_count,
        fields_needing_review=fields_needing,
        worker_active=sync_repo.has_active_run(portal),
        last_sync_run=None,
        last_error=last_run.last_error if last_run else None,
        schedule_enabled=settings.bitrix_import_schedule_enabled,
        schedule_interval_minutes=settings.bitrix_import_schedule_interval_minutes,
    )
    return templates.TemplateResponse(
        request,
        "bitrix_import.html",
        {"dashboard": dashboard, "format_dt": format_local_dt},
    )


@router.get("/bitrix-import/entities/{entity_type_id}", response_class=HTMLResponse)
def bitrix_entities_list(
    request: Request,
    entity_type_id: int,
    page: int = 1,
    search: str | None = None,
    db: Session = Depends(get_db),
):
    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    repo = CrmRepository(db, portal)
    page_size = 50
    entities, total = repo.list_entities_paginated(entity_type_id, page, page_size, search)
    return templates.TemplateResponse(
        request,
        "bitrix_entities.html",
        {
            "entities": entities,
            "entity_type_id": entity_type_id,
            "entity_label": ENTITY_LABELS.get(entity_type_id, f"Тип {entity_type_id}"),
            "page": page,
            "page_size": page_size,
            "total": total,
            "search": search,
            "format_dt": format_local_dt,
        },
    )


@router.get("/bitrix-import/entities/{entity_type_id}/{entity_id}", response_class=HTMLResponse)
def bitrix_entity_detail(
    request: Request,
    entity_type_id: int,
    entity_id: int,
    db: Session = Depends(get_db),
):
    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    repo = CrmRepository(db, portal)
    entity = repo.get_entity(entity_type_id, entity_id)
    if not entity:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Не найдено")
    versions = repo.get_entity_versions(entity_type_id, entity_id)
    children = repo.get_child_records(entity_type_id, entity_id)

    contact_repo = ContactRepository(db, portal)
    linked_contacts = []
    linked_parents = []
    if entity_type_id in (1, 2):
        linked_contacts = contact_repo.get_contacts_for_parent(entity_type_id, entity_id)
    elif entity_type_id == 3:
        linked_parents = contact_repo.get_links_for_contact(entity_id)

    return templates.TemplateResponse(
        request,
        "bitrix_entity_detail.html",
        {
            "entity": entity,
            "versions": versions,
            "child_records": children,
            "raw_json": json.dumps(entity.raw_payload, ensure_ascii=False, indent=2),
            "linked_contacts": linked_contacts,
            "linked_parents": linked_parents,
        },
    )


@router.get("/bitrix-import/fields", response_class=HTMLResponse)
def bitrix_fields_page(request: Request, db: Session = Depends(get_db)):
    from app.models import CrmFieldDefinition
    from sqlalchemy import select

    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    repo = CrmRepository(db, portal)
    fields = list(
        db.scalars(
            select(CrmFieldDefinition)
            .where(CrmFieldDefinition.portal_id == portal, CrmFieldDefinition.is_active.is_(True))
            .order_by(CrmFieldDefinition.entity_type_id, CrmFieldDefinition.original_field_name)
        )
    )
    field_ids = [f.id for f in fields]
    profiles = repo.get_value_profiles_by_field_ids(field_ids)
    field_data = []
    for f in fields:
        sem = repo.get_semantic(f.id)
        prof = profiles.get(f.id)
        field_data.append(type("F", (), {
            "original_field_name": f.original_field_name,
            "title": f.title,
            "field_type": f.field_type,
            "is_custom": f.is_custom,
            "discovered_from_payload": getattr(f, "discovered_from_payload", False),
            "semantic": sem,
            "filled_count": prof.filled_count if prof else 0,
        })())
    return templates.TemplateResponse(request, "bitrix_fields.html", {"fields": field_data})


@router.get("/bitrix-import/dictionaries", response_class=HTMLResponse)
def bitrix_dictionaries_page(request: Request, db: Session = Depends(get_db)):
    from app.models import CrmDictionary, CrmDictionaryEntry
    from sqlalchemy import func, select

    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    dicts = list(db.scalars(select(CrmDictionary).where(CrmDictionary.portal_id == portal)))
    result = []
    for d in dicts:
        cnt = db.scalar(
            select(func.count()).select_from(CrmDictionaryEntry).where(
                CrmDictionaryEntry.dictionary_id == d.id, CrmDictionaryEntry.is_active.is_(True)
            )
        ) or 0
        result.append(type("D", (), {"dictionary_code": d.dictionary_code, "title": d.title, "source_type": d.source_type, "entries_count": cnt, "id": d.id})())
    return templates.TemplateResponse(request, "bitrix_dictionaries.html", {"dictionaries": result})


@router.get("/bitrix-import/dictionaries/{dictionary_id}", response_class=HTMLResponse)
def bitrix_dictionary_detail(
    request: Request,
    dictionary_id: int,
    db: Session = Depends(get_db),
):
    from fastapi import HTTPException

    from app.models import CrmDictionary, CrmDictionaryEntry
    from sqlalchemy import select

    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    d = db.get(CrmDictionary, dictionary_id)
    if not d or d.portal_id != portal:
        raise HTTPException(status_code=404, detail="Справочник не найден")
    entries = list(
        db.scalars(
            select(CrmDictionaryEntry)
            .where(
                CrmDictionaryEntry.dictionary_id == dictionary_id,
                CrmDictionaryEntry.is_active.is_(True),
            )
            .order_by(CrmDictionaryEntry.sort_order, CrmDictionaryEntry.raw_value)
        )
    )
    return templates.TemplateResponse(
        request,
        "bitrix_dictionary_detail.html",
        {
            "dictionary": d,
            "entries": entries,
            "entries_count": len(entries),
        },
    )


@router.get("/bitrix-import/runs", response_class=HTMLResponse)
def bitrix_runs_page(request: Request, db: Session = Depends(get_db)):
    from app.models import SyncRun
    from sqlalchemy import select

    settings = get_app_settings(db)
    portal = portal_id_from_webhook(settings.bitrix_webhook_url)
    runs = list(db.scalars(select(SyncRun).where(SyncRun.portal_id == portal).order_by(SyncRun.created_at.desc()).limit(100)))
    return templates.TemplateResponse(
        request,
        "bitrix_runs.html",
        {"runs": runs, "format_dt": format_local_dt},
    )


@router.get("/exports/{job_id}", response_class=HTMLResponse)
def export_detail(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    event_log = json.loads(job.event_log_json) if job else []
    statistics = json.loads(job.statistics_json) if job else {}
    parameters: dict = {}
    if job and job.parameters_json:
        parameters = json.loads(job.parameters_json)
        parameters.pop("_mode", None)
    return templates.TemplateResponse(
        request,
        "export_detail.html",
        {
            "job": job,
            "event_log": event_log,
            "statistics": statistics,
            "parameters": parameters,
            "format_dt": format_local_dt,
            "has_json_download": _has_json_download(job),
        },
    )


@router.get("/call-results", response_class=HTMLResponse)
def call_results_page(request: Request, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    from app.repositories.call_result_repository import CallResultRepository
    from app.services.auth_service import resolve_portal_id

    portal_id = resolve_portal_id(settings)
    imports = CallResultRepository(db, portal_id).list_imports(limit=15)
    return templates.TemplateResponse(
        request,
        "call_results.html",
        {"imports": imports, "format_dt": format_local_dt},
    )


@router.get("/call-results/imports/{import_id}", response_class=HTMLResponse)
def call_result_import_page(import_id: int, request: Request, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    from app.repositories.call_result_repository import CallResultRepository
    from app.services.auth_service import resolve_portal_id

    portal_id = resolve_portal_id(settings)
    imp = CallResultRepository(db, portal_id).get_import(import_id)
    if imp is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Import not found")
    return templates.TemplateResponse(
        request,
        "call_result_import.html",
        {"import_id": import_id, "import_rec": imp, "format_dt": format_local_dt},
    )
