"""Admin API for Bitrix CRM import."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_app_settings
from app.models import (
    CrmDictionary,
    CrmDictionaryEntry,
    CrmEntity,
    CrmFieldDefinition,
    CrmFieldSemantic,
    ENTITY_DEAL,
    ENTITY_LEAD,
    SyncRun,
)
from app.repositories.crm_repository import CrmRepository
from app.repositories.sync_repository import SyncRepository
from app.schemas_bitrix import (
    DashboardResponse,
    DictionaryEntryPatch,
    DictionaryResponse,
    EntityDetailResponse,
    EntityListResponse,
    FieldResponse,
    FieldSemanticPatch,
    ImportCreateRequest,
    ImportListResponse,
    ImportResponse,
    MessageResponse,
)
from app.services.bitrix_client import BitrixClient
from app.services.bitrix_import.import_queue_service import (
    ConcurrentImportError,
    FullImportNotConfirmedError,
    enqueue_import,
)
from app.utils.portal import portal_id_from_webhook

router = APIRouter(prefix="/admin/bitrix", tags=["admin-bitrix"])


def _portal(db: Session) -> str:
    settings = get_app_settings(db)
    return portal_id_from_webhook(settings.bitrix_webhook_url)


def _run_to_response(run: SyncRun) -> ImportResponse:
    return ImportResponse(
        id=run.id,
        portal_id=run.portal_id,
        mode=run.mode,
        status=run.status,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
        requested_by=run.requested_by,
        current_phase=run.current_phase,
        processed_count=run.processed_count,
        created_count=run.created_count,
        updated_count=run.updated_count,
        unchanged_count=run.unchanged_count,
        deleted_count=run.deleted_count,
        failed_count=run.failed_count,
        api_requests_count=run.api_requests_count,
        ai_requests_count=run.ai_requests_count,
        last_error=run.last_error,
        statistics=run.statistics_json or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    portal = _portal(db)
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
    cp = sync_repo.get_checkpoint(portal, "entities", ENTITY_LEAD)
    last_run = db.scalar(select(SyncRun).order_by(SyncRun.created_at.desc()).limit(1))
    active = sync_repo.has_active_run(portal)

    fields_needing = db.scalar(
        select(func.count())
        .select_from(CrmFieldSemantic)
        .join(CrmFieldDefinition, CrmFieldDefinition.id == CrmFieldSemantic.field_definition_id)
        .where(CrmFieldDefinition.portal_id == portal, CrmFieldSemantic.needs_review.is_(True))
    ) or 0

    dict_count = db.scalar(
        select(func.count()).select_from(CrmDictionary).where(CrmDictionary.portal_id == portal)
    ) or 0

    return DashboardResponse(
        bitrix_connected=bitrix_ok,
        portal_id=portal,
        last_full_import=last_full.finished_at if last_full else None,
        last_incremental_import=last_incr.finished_at if last_incr else None,
        checkpoint={
            "cursor_time": cp.cursor_time.isoformat() if cp and cp.cursor_time else None,
            "cursor_id": cp.cursor_id if cp else None,
        } if cp else None,
        leads_count=crm_repo.count_entities(ENTITY_LEAD, is_deleted=False),
        deals_count=crm_repo.count_entities(ENTITY_DEAL, is_deleted=False),
        deleted_count=crm_repo.count_entities(is_deleted=True),
        fields_count=crm_repo.count_fields(),
        custom_fields_count=crm_repo.count_fields(custom_only=True),
        dictionaries_count=dict_count,
        fields_needing_review=fields_needing,
        worker_active=active,
        last_sync_run=_run_to_response(last_run) if last_run else None,
        last_error=last_run.last_error if last_run else None,
        schedule_enabled=settings.bitrix_import_schedule_enabled,
        schedule_interval_minutes=settings.bitrix_import_schedule_interval_minutes,
    )


@router.post("/imports", response_model=MessageResponse)
def create_import(body: ImportCreateRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    if not settings.bitrix_webhook_url:
        raise HTTPException(status_code=400, detail="URL вебхука не настроен")

    portal = _portal(db)
    sync_repo = SyncRepository(db)

    try:
        run = enqueue_import(
            sync_repo,
            portal,
            mode=body.mode,
            analyze_metadata=body.analyze_metadata,
            confirm_full=body.confirm_full,
        )
    except FullImportNotConfirmedError:
        raise HTTPException(status_code=400, detail="Для полного импорта требуется confirm_full=true")
    except ConcurrentImportError:
        raise HTTPException(status_code=409, detail="Импорт уже выполняется")

    return MessageResponse(message="Импорт поставлен в очередь", import_id=run.id)


@router.get("/imports", response_model=ImportListResponse)
def list_imports(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    portal = _portal(db)
    q = select(SyncRun).where(SyncRun.portal_id == portal).order_by(SyncRun.created_at.desc())
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    runs = list(db.scalars(q.offset((page - 1) * page_size).limit(page_size)))
    return ImportListResponse(items=[_run_to_response(r) for r in runs], total=total)


@router.get("/imports/{import_id}", response_model=ImportResponse)
def get_import(import_id: int, db: Session = Depends(get_db)):
    run = SyncRepository(db).get_run(import_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    return _run_to_response(run)


@router.get("/imports/{import_id}/status")
async def import_status(import_id: int, request: Request, db: Session = Depends(get_db)):
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return StreamingResponse(
            _sse_import(import_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    run = SyncRepository(db).get_run(import_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    return _run_to_response(run)


async def _sse_import(import_id: int):
    from app.database import SessionLocal

    while True:
        db = SessionLocal()
        try:
            run = SyncRepository(db).get_run(import_id)
            if not run:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                break
            payload = _run_to_response(run).model_dump(mode="json")
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if run.status in ("completed", "failed", "cancelled"):
                break
        finally:
            db.close()
        await asyncio.sleep(1.5)


@router.post("/imports/{import_id}/cancel", response_model=ImportResponse)
def cancel_import(import_id: int, db: Session = Depends(get_db)):
    run = SyncRepository(db).cancel_run(import_id)
    if not run:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    return _run_to_response(run)


@router.post("/reconciliation", response_model=MessageResponse)
def start_reconciliation(db: Session = Depends(get_db)):
    portal = _portal(db)
    sync_repo = SyncRepository(db)
    if sync_repo.has_active_run(portal):
        raise HTTPException(status_code=409, detail="Импорт уже выполняется")
    run = sync_repo.create_run(portal, "reconciliation")
    return MessageResponse(message="Сверка поставлена в очередь", import_id=run.id)


@router.post("/metadata/analyze", response_model=MessageResponse)
def start_metadata_analyze(db: Session = Depends(get_db)):
    portal = _portal(db)
    sync_repo = SyncRepository(db)
    if sync_repo.has_active_run(portal):
        raise HTTPException(status_code=409, detail="Импорт уже выполняется")
    run = sync_repo.create_run(portal, "ai_reanalysis", statistics={"analyze_metadata": True})
    return MessageResponse(message="AI-анализ поставлен в очередь", import_id=run.id)


@router.get("/entities", response_model=EntityListResponse)
def list_entities(
    entity_type_id: int = Query(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    search: str | None = None,
    stage_id: str | None = None,
    category_id: int | None = None,
    assigned_by_id: int | None = None,
    is_deleted: bool | None = None,
    sort: str = "updated_time",
    order: str = "desc",
    db: Session = Depends(get_db),
):
    portal = _portal(db)
    repo = CrmRepository(db, portal)
    entities, total = repo.list_entities_paginated(
        entity_type_id, page, page_size, search, stage_id, category_id,
        assigned_by_id, is_deleted, sort, order,
    )
    items = [
        {
            "entity_id": e.entity_id,
            "entity_type_id": e.entity_type_id,
            "title": e.title,
            "stage_id": e.stage_id,
            "category_id": e.category_id,
            "assigned_by_id": e.assigned_by_id,
            "updated_time": e.updated_time.isoformat() if e.updated_time else None,
            "is_deleted": e.is_deleted,
        }
        for e in entities
    ]
    return EntityListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get("/entities/{entity_type_id}/{entity_id}", response_model=EntityDetailResponse)
def get_entity(entity_type_id: int, entity_id: int, db: Session = Depends(get_db)):
    portal = _portal(db)
    repo = CrmRepository(db, portal)
    entity = repo.get_entity(entity_type_id, entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Сущность не найдена")
    versions = repo.get_entity_versions(entity_type_id, entity_id)
    children = repo.get_child_records(entity_type_id, entity_id)
    return EntityDetailResponse(
        entity={
            "entity_id": entity.entity_id,
            "entity_type_id": entity.entity_type_id,
            "title": entity.title,
            "raw_payload": entity.raw_payload,
            "is_deleted": entity.is_deleted,
            "payload_hash": entity.payload_hash,
        },
        versions=[
            {"id": v.id, "payload_hash": v.payload_hash, "valid_from": v.valid_from.isoformat(), "change_source": v.change_source}
            for v in versions
        ],
        child_records=[
            {"record_type": c.record_type, "external_id": c.external_id, "raw_payload": c.raw_payload}
            for c in children
        ],
        field_values=[],
    )


@router.get("/fields", response_model=list[FieldResponse])
def list_fields(
    entity_type_id: int | None = None,
    custom_only: bool = False,
    db: Session = Depends(get_db),
):
    portal = _portal(db)
    q = select(CrmFieldDefinition).where(
        CrmFieldDefinition.portal_id == portal,
        CrmFieldDefinition.is_active.is_(True),
    )
    if entity_type_id is not None:
        q = q.where(CrmFieldDefinition.entity_type_id == entity_type_id)
    if custom_only:
        q = q.where(CrmFieldDefinition.is_custom.is_(True))
    fields = list(db.scalars(q.order_by(CrmFieldDefinition.original_field_name)))
    repo = CrmRepository(db, portal)
    result = []
    for f in fields:
        sem = repo.get_semantic(f.id)
        result.append(
            FieldResponse(
                id=f.id,
                entity_type_id=f.entity_type_id,
                original_field_name=f.original_field_name,
                title=f.title,
                field_type=f.field_type,
                is_custom=f.is_custom,
                is_multiple=f.is_multiple,
                is_required=f.is_required,
                is_active=f.is_active,
                definition_hash=f.definition_hash,
                last_seen_at=f.last_seen_at,
                semantic={
                    "display_name": sem.display_name,
                    "short_description": sem.short_description,
                    "confidence": sem.confidence,
                    "needs_review": sem.needs_review,
                    "is_manual": sem.is_manual,
                } if sem else None,
            )
        )
    return result


@router.get("/fields/{field_id}", response_model=FieldResponse)
def get_field(field_id: int, db: Session = Depends(get_db)):
    f = db.get(CrmFieldDefinition, field_id)
    if not f:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    sem = CrmRepository(db, f.portal_id).get_semantic(f.id)
    return FieldResponse(
        id=f.id,
        entity_type_id=f.entity_type_id,
        original_field_name=f.original_field_name,
        title=f.title,
        field_type=f.field_type,
        is_custom=f.is_custom,
        is_multiple=f.is_multiple,
        is_required=f.is_required,
        is_active=f.is_active,
        definition_hash=f.definition_hash,
        last_seen_at=f.last_seen_at,
        semantic={
            "display_name": sem.display_name,
            "short_description": sem.short_description,
            "detailed_description": sem.detailed_description,
            "confidence": sem.confidence,
            "needs_review": sem.needs_review,
            "is_manual": sem.is_manual,
        } if sem else None,
    )


@router.patch("/fields/{field_id}/semantics")
def patch_field_semantics(field_id: int, body: FieldSemanticPatch, db: Session = Depends(get_db)):
    f = db.get(CrmFieldDefinition, field_id)
    if not f:
        raise HTTPException(status_code=404, detail="Поле не найдено")
    from app.models import utcnow

    sem = CrmFieldSemantic(
        field_definition_id=field_id,
        language="ru",
        display_name=body.display_name,
        short_description=body.short_description,
        detailed_description=body.detailed_description,
        business_purpose=body.business_purpose,
        is_manual=True,
        needs_review=False,
        confidence=1.0,
        reviewed_at=utcnow(),
        reviewed_by=body.reviewed_by,
    )
    db.add(sem)
    db.commit()
    return {"message": "Описание сохранено"}


@router.get("/dictionaries", response_model=list[DictionaryResponse])
def list_dictionaries(
    entity_type_id: int | None = None,
    db: Session = Depends(get_db),
):
    portal = _portal(db)
    q = select(CrmDictionary).where(CrmDictionary.portal_id == portal, CrmDictionary.is_active.is_(True))
    if entity_type_id is not None:
        q = q.where(CrmDictionary.entity_type_id == entity_type_id)
    dicts = list(db.scalars(q))
    result = []
    for d in dicts:
        cnt = db.scalar(
            select(func.count()).select_from(CrmDictionaryEntry).where(
                CrmDictionaryEntry.dictionary_id == d.id,
                CrmDictionaryEntry.is_active.is_(True),
            )
        ) or 0
        result.append(
            DictionaryResponse(
                id=d.id,
                dictionary_code=d.dictionary_code,
                title=d.title,
                source_type=d.source_type,
                entity_type_id=d.entity_type_id,
                field_definition_id=d.field_definition_id,
                is_active=d.is_active,
                entries_count=cnt,
            )
        )
    return result


@router.get("/dictionaries/{dictionary_id}")
def get_dictionary(dictionary_id: int, db: Session = Depends(get_db)):
    d = db.get(CrmDictionary, dictionary_id)
    if not d:
        raise HTTPException(status_code=404, detail="Справочник не найден")
    entries = list(
        db.scalars(
            select(CrmDictionaryEntry).where(
                CrmDictionaryEntry.dictionary_id == dictionary_id,
                CrmDictionaryEntry.is_active.is_(True),
            )
        )
    )
    return {
        "dictionary": DictionaryResponse(
            id=d.id,
            dictionary_code=d.dictionary_code,
            title=d.title,
            source_type=d.source_type,
            entity_type_id=d.entity_type_id,
            field_definition_id=d.field_definition_id,
            is_active=d.is_active,
            entries_count=len(entries),
        ),
        "entries": [
            {
                "id": e.id,
                "external_id": e.external_id,
                "raw_value": e.raw_value,
                "normalized_value": e.normalized_value,
                "description": e.description,
                "confidence": e.confidence,
                "needs_review": e.needs_review,
            }
            for e in entries
        ],
    }


@router.patch("/dictionary-entries/{entry_id}")
def patch_dictionary_entry(entry_id: int, body: DictionaryEntryPatch, db: Session = Depends(get_db)):
    entry = db.get(CrmDictionaryEntry, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Элемент не найден")
    if body.normalized_value is not None:
        entry.normalized_value = body.normalized_value
    if body.description is not None:
        entry.description = body.description
    entry.needs_review = False
    db.commit()
    return {"message": "Элемент обновлён"}
