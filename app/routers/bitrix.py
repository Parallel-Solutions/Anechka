"""Bitrix24 API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_app_settings
from app.exceptions import AppError, ExportValidationError
from app.schemas import CategoryItem, RegionSearchResult, StageItem, UserItem
from app.services.bitrix_client import BitrixClient

router = APIRouter(prefix="/api", tags=["bitrix"])


def _client(db: Session) -> BitrixClient:
    settings = get_app_settings(db)
    if not settings.bitrix_webhook_url:
        raise HTTPException(status_code=400, detail="URL вебхука не настроен")
    return BitrixClient(settings)


@router.get("/categories", response_model=list[CategoryItem])
def list_categories(db: Session = Depends(get_db)):
    try:
        cats = _client(db).get_categories()
        return [CategoryItem(id=c["id"], name=c["name"]) for c in cats]
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc


@router.get("/categories/{category_id}/stages", response_model=list[StageItem])
def list_stages(category_id: int, db: Session = Depends(get_db)):
    try:
        stages = _client(db).get_stages(category_id)
        return [
            StageItem(id=s["id"], name=s["name"], category_id=category_id) for s in stages
        ]
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc


@router.get("/lead-statuses", response_model=list[StageItem])
def list_lead_statuses(db: Session = Depends(get_db)):
    try:
        statuses = _client(db).get_lead_statuses()
        return [
            StageItem(id=s["id"], name=s["name"], category_id=0) for s in statuses
        ]
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc


@router.get("/users", response_model=list[UserItem])
def list_users(db: Session = Depends(get_db)):
    try:
        users = _client(db).get_users()
        return [UserItem(id=u["id"], name=u["name"]) for u in users]
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc


@router.get("/regions", response_model=list[RegionSearchResult])
def list_regions(
    iblock_id: int = Query(default=49),
    db: Session = Depends(get_db),
):
    settings = get_app_settings(db)
    if not settings.bitrix_webhook_url:
        return []
    try:
        regions = BitrixClient(settings).list_regions(iblock_id)
        return [RegionSearchResult(id=r["id"], name=r["name"]) for r in regions]
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc


@router.get("/regions/search", response_model=list[RegionSearchResult])
def search_regions(
    name: str = Query(min_length=1),
    iblock_id: int = Query(default=49),
    db: Session = Depends(get_db),
):
    try:
        regions = _client(db).find_regions(name, iblock_id)
        if not regions:
            raise HTTPException(status_code=404, detail="Указанный регион не найден")
        return [RegionSearchResult(id=r["id"], name=r["name"]) for r in regions]
    except HTTPException:
        raise
    except AppError as exc:
        raise HTTPException(status_code=502, detail=exc.user_message) from exc
