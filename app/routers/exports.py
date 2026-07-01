"""Export job endpoints."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import Settings, get_export_dir
from app.database import get_db
from app.dependencies import get_app_settings
from app.exceptions import ExportValidationError
from app.services.auth_service import resolve_portal_id
from app.services.lpr_service import load_lpr_config
from app.services.lpr_tomoru_service import LprTomoruService
from app.models import ExportJob
from app.schemas import (
    CategoryFullExportRequest,
    ExportDealItem,
    ExportDealsResponse,
    ExportJobResponse,
    MessageResponse,
    RegionExportRequest,
    StageExportRequest,
    TomoruContactSelectionRequest,
    TomoruExportRequest,
)
from app.services.bitrix_client import BitrixClient
from app.services.export_deals_service import ExportDealsService
from app.services.tomoru_contact_preferences import set_deal as save_tomoru_contact_selection
from app.services.job_service import JobService
from app.services.json_export_service import build_json_from_xlsx, write_export_json
from app.services.security_service import validate_download_path

router = APIRouter(tags=["exports"])
job_service = JobService()


def _job_to_response(job: ExportJob) -> ExportJobResponse:
    return ExportJobResponse(
        id=job.id,
        mode=job.mode,
        status=job.status,
        progress_current=job.progress_current,
        progress_total=job.progress_total,
        progress_percent=job.progress_percent,
        current_step=job.current_step,
        result_file=job.result_file,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        error_message=job.error_message,
        cancel_requested=job.cancel_requested,
        statistics=json.loads(job.statistics_json or "{}"),
        event_log=json.loads(job.event_log_json or "[]"),
        parameters=json.loads(job.parameters_json or "{}"),
    )


@router.post("/exports/region", response_model=MessageResponse)
def create_region_export(body: RegionExportRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    if body.limit > settings.max_export_size:
        raise HTTPException(
            status_code=400,
            detail=f"Лимит не может превышать {settings.max_export_size}",
        )
    if not body.region_id:
        raise HTTPException(status_code=400, detail="Не указан ID региона")
    job = job_service.create_job(db, "region", body.model_dump())
    return MessageResponse(message="Задача создана", job_id=job.id)


@router.post("/exports/stage", response_model=MessageResponse)
def create_stage_export(body: StageExportRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    if body.limit > settings.max_export_size:
        raise HTTPException(
            status_code=400,
            detail=f"Лимит не может превышать {settings.max_export_size}",
        )
    if settings.bitrix_webhook_url:
        client = BitrixClient(settings)
        stages = client.get_stages(body.category_id)
        if not any(s["id"] == body.stage_id for s in stages):
            raise HTTPException(
                status_code=400,
                detail="Стадия не принадлежит выбранной категории",
            )
    job = job_service.create_job(db, "stage", body.model_dump())
    return MessageResponse(message="Задача создана", job_id=job.id)


def _validate_tomoru_export(body: TomoruExportRequest, settings: Settings) -> None:
    if body.entity_type == "deal" and body.stage_ids and settings.bitrix_webhook_url:
        client = BitrixClient(settings)
        stages = client.get_stages(body.category_id)
        valid_ids = {s["id"] for s in stages}
        invalid = [s for s in body.stage_ids if s not in valid_ids]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail="Стадия не принадлежит выбранной воронке",
            )
    if body.entity_type == "lead" and body.stage_ids and settings.bitrix_webhook_url:
        client = BitrixClient(settings)
        statuses = client.get_lead_statuses()
        valid_ids = {s["id"] for s in statuses}
        invalid = [s for s in body.stage_ids if s not in valid_ids]
        if invalid:
            raise HTTPException(status_code=400, detail="Неверный статус лида")


@router.post("/exports/tomoru", response_model=MessageResponse)
def create_tomoru_export(body: TomoruExportRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    _validate_tomoru_export(body, settings)
    params = body.model_dump(mode="json")
    job = job_service.create_job(db, "region_lpr", params)
    return MessageResponse(message="Задача создана", job_id=job.id)


@router.post("/exports/tomoru/download")
def download_tomoru_export(body: TomoruExportRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    _validate_tomoru_export(body, settings)
    params = body.model_dump(mode="json")
    lpr_service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=load_lpr_config(db),
        db=db,
        portal_id=resolve_portal_id(settings),
    )
    try:
        result_path = lpr_service.run_lpr_tomoru_export(params)
    except ExportValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    export_dir = get_export_dir(settings)
    try:
        path = validate_download_path(export_dir, result_path)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=path.name,
        media_type="text/csv",
        headers={
            "X-Export-Matched-Total": str(lpr_service.last_matched_total),
        },
    )


def _deals_result_to_response(result) -> ExportDealsResponse:
    return ExportDealsResponse(
        total=result.total,
        deals=[ExportDealItem(**d) for d in result.deals],
        available=result.available,
        source=result.source,  # type: ignore[arg-type]
        offset=result.offset,
        limit=result.limit,
        note=result.note,
        matched_total=result.matched_total,
        truncated=result.truncated,
    )


@router.put("/api/tomoru/deals/{deal_id}/contact-selection", status_code=204)
def save_tomoru_deal_contact_selection(
    deal_id: int,
    body: TomoruContactSelectionRequest,
    db: Session = Depends(get_db),
):
    settings = get_app_settings(db)
    portal_id = resolve_portal_id(settings)
    save_tomoru_contact_selection(db, portal_id, deal_id, body.contact_ids)
    return Response(status_code=204)


@router.get("/api/tomoru/deals", response_model=ExportDealsResponse)
def tomoru_deals_preview(
    entity_type: Literal["deal", "lead"] = Query(default="deal"),
    category_id: int = Query(default=15),
    stage_id: list[str] = Query(default=[]),
    region_id: list[int] = Query(default=[]),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    page_size: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    settings = get_app_settings(db)
    result = ExportDealsService(db, settings).list_tomoru_deals(
        entity_type=entity_type,
        category_id=category_id,
        stage_ids=stage_id or None,
        region_ids=region_id or None,
        date_from=date_from,
        date_to=date_to,
        offset=offset,
        limit=page_size,
    )
    return _deals_result_to_response(result)


@router.post("/exports/category-full", response_model=MessageResponse)
def create_category_full_export(body: CategoryFullExportRequest, db: Session = Depends(get_db)):
    settings = get_app_settings(db)
    if body.limit > settings.max_export_size:
        raise HTTPException(
            status_code=400,
            detail=f"Лимит не может превышать {settings.max_export_size}",
        )
    if settings.bitrix_webhook_url:
        client = BitrixClient(settings)
        categories = client.get_categories()
        if not any(c["id"] == body.category_id for c in categories):
            raise HTTPException(status_code=400, detail="Категория не найдена")
    job = job_service.create_job(db, "category_full", body.model_dump())
    return MessageResponse(message="Задача создана", job_id=job.id)


@router.get("/exports", response_model=list[ExportJobResponse])
def list_exports(db: Session = Depends(get_db)):
    jobs = db.query(ExportJob).order_by(ExportJob.created_at.desc()).all()
    return [_job_to_response(j) for j in jobs]


@router.get("/exports/{job_id}", response_model=ExportJobResponse)
def get_export(job_id: int, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return _job_to_response(job)


@router.get("/api/exports/{job_id}/deals", response_model=ExportDealsResponse)
def export_deals(
    job_id: int,
    source: Literal["filter", "file"] = Query(default="filter"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    if source == "file" and job.status != "completed":
        raise HTTPException(
            status_code=400,
            detail="Список сделок из файла доступен только для завершённых выгрузок",
        )

    settings = get_app_settings(db)
    result = ExportDealsService(db, settings).list_deals(
        job, source=source, offset=offset, limit=limit
    )
    return _deals_result_to_response(result)


@router.get("/api/exports/{job_id}/status")
async def export_status(job_id: int, request: Request, db: Session = Depends(get_db)):
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        return StreamingResponse(
            _sse_generator(job_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return _job_to_response(job)


async def _sse_generator(job_id: int):
    import asyncio

    from app.database import SessionLocal

    while True:
        db = SessionLocal()
        try:
            job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
            if not job:
                yield f"data: {json.dumps({'error': 'not found'})}\n\n"
                break
            payload = _job_to_response(job).model_dump(mode="json")
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if job.status in ("completed", "failed", "cancelled"):
                break
        finally:
            db.close()
        await asyncio.sleep(1.5)


@router.post("/api/exports/{job_id}/cancel", response_model=ExportJobResponse)
def cancel_export(job_id: int, db: Session = Depends(get_db)):
    try:
        job = job_service.cancel_job(db, job_id)
        return _job_to_response(job)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/exports/{job_id}/retry", response_model=MessageResponse)
def retry_export(job_id: int, db: Session = Depends(get_db)):
    try:
        job = job_service.retry_job(db, job_id)
        return MessageResponse(message="Задача перезапущена", job_id=job.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/exports/{job_id}/download")
def download_export(job_id: int, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job or job.status != "completed" or not job.result_file:
        raise HTTPException(status_code=404, detail="Файл недоступен")
    settings = get_app_settings(db)
    export_dir = get_export_dir(settings)
    try:
        path = validate_download_path(export_dir, job.result_file)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return FileResponse(
        path,
        filename=path.name,
        media_type=(
            "text/csv"
            if path.suffix.lower() == ".csv"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )


@router.get("/exports/{job_id}/download/json")
def download_export_json(job_id: int, db: Session = Depends(get_db)):
    job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
    if not job or job.status != "completed" or not job.result_file:
        raise HTTPException(status_code=404, detail="Файл недоступен")
    settings = get_app_settings(db)
    export_dir = get_export_dir(settings)
    json_path = Path(job.result_file).with_suffix(".json")
    if json_path.is_file():
        try:
            path = validate_download_path(export_dir, str(json_path))
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return FileResponse(
            path,
            filename=path.name,
            media_type="application/json",
        )

    try:
        xlsx_path = validate_download_path(export_dir, job.result_file)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        payload = build_json_from_xlsx(xlsx_path, job.mode)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Не удалось сформировать JSON из XLSX") from exc

    cached_path = write_export_json(xlsx_path, payload)
    return FileResponse(
        cached_path,
        filename=cached_path.name,
        media_type="application/json",
    )
