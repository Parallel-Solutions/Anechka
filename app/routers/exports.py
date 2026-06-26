"""Export job endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.config import get_export_dir
from app.database import get_db
from app.dependencies import get_app_settings
from app.exceptions import ExportValidationError
from app.models import ExportJob
from app.schemas import (
    CategoryFullExportRequest,
    ExportJobResponse,
    MessageResponse,
    RegionExportRequest,
    StageExportRequest,
)
from app.services.bitrix_client import BitrixClient
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
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
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
