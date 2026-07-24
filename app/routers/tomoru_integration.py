"""Tomoru microservice callbacks and guarded retry-event dispatch."""

from __future__ import annotations

import secrets
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import BASE_DIR
from app.dependencies import get_app_settings, get_session, require_role
from app.models import CallRetryQueueEntry, utcnow
from app.services.auth_service import resolve_portal_id
from app.services.call_results.retry_queue_gateway import RetryQueueGateway
from app.services.call_results.tomoru_api import TomoruBatchDispatcher
from app.services.call_results.tomoru_event_dispatcher import (
    TomoruCallbackUrlError,
    TomoruEventDispatcher,
    validate_tomoru_callback_url,
)
from app.services.call_results.tomoru_integration_store import TomoruIntegrationStore

router = APIRouter(tags=["tomoru-integration"])


class TomoruSubscribeRequest(BaseModel):
    tomoruCallbackUrl: str = Field(min_length=1)


class TomoruEventStatus(BaseModel):
    trackingId: str = Field(min_length=1)
    event: str = Field(min_length=1)
    status: Literal["sent_to_bot", "processed", "rejected"]


class TomoruEventStatusRequest(BaseModel):
    eventStatus: TomoruEventStatus


class TomoruDispatchRequest(BaseModel):
    import_id: int | None = None
    entry_ids: list[int] | None = None
    dry_run: bool = True
    include_future: bool = True
    limit: int = Field(default=500, ge=1, le=5000)


class TomoruBatchDispatchRequest(BaseModel):
    import_id: int | None = None
    entry_ids: list[int] | None = None
    dry_run: bool = True
    launch: bool = False
    limit: int = Field(default=500, ge=1, le=5000)


def _require_webhook_secret(settings, provided: str | None) -> None:
    configured = settings.tomoru_webhook_secret
    if not configured:
        raise HTTPException(status_code=503, detail="TOMORU_WEBHOOK_SECRET is not configured")
    if not provided or not secrets.compare_digest(configured, provided):
        raise HTTPException(status_code=401, detail="Invalid Tomoru webhook secret")


@router.get("/tomoru-hooks/openapi.yaml", response_class=PlainTextResponse)
def tomoru_openapi(db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    template = (BASE_DIR / "docs" / "tomoru-microservice.openapi.yaml").read_text(encoding="utf-8")
    base_url = (settings.tomoru_public_base_url or "https://anechka.example.com").rstrip("/")
    return template.replace("https://anechka.example.com", base_url)


@router.post("/tomoru-hooks/subscribe")
def tomoru_subscribe(
    body: TomoruSubscribeRequest,
    x_anechka_tomoru_secret: str | None = Header(
        default=None,
        alias="X-Anechka-Tomoru-Secret",
    ),
    db: Session = Depends(get_session),
):
    settings = get_app_settings(db)
    _require_webhook_secret(settings, x_anechka_tomoru_secret)
    try:
        callback_url = validate_tomoru_callback_url(
            body.tomoruCallbackUrl,
            settings.tomoru_callback_allowed_hosts,
        )
    except TomoruCallbackUrlError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    portal_id = resolve_portal_id(settings)
    state = TomoruIntegrationStore(db, portal_id).save_callback(
        callback_url,
        bot_id=settings.tomoru_bot_id,
    )
    db.commit()
    return {
        "ok": True,
        "callback_host": urlparse(state.callback_url).hostname,
        "bot_id_configured": bool(state.bot_id),
        "external_calls_enabled": settings.tomoru_events_enabled,
    }


@router.post("/tomoru-hooks/event-status")
def tomoru_event_status(
    body: TomoruEventStatusRequest,
    x_anechka_tomoru_secret: str | None = Header(
        default=None,
        alias="X-Anechka-Tomoru-Secret",
    ),
    db: Session = Depends(get_session),
):
    settings = get_app_settings(db)
    _require_webhook_secret(settings, x_anechka_tomoru_secret)
    portal_id = resolve_portal_id(settings)
    event_status = body.eventStatus
    entry = db.scalar(
        select(CallRetryQueueEntry).where(
            CallRetryQueueEntry.portal_id == portal_id,
            CallRetryQueueEntry.idempotency_key == event_status.trackingId,
        )
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Retry queue entry not found")
    if event_status.status == "sent_to_bot":
        entry.status = "sent_to_tomoru"
        entry.dispatched_at = entry.dispatched_at or utcnow()
        entry.last_error = None
    elif event_status.status == "processed":
        entry.status = "completed"
        entry.last_error = None
    else:
        entry.status = "failed"
        entry.last_error = "Tomoru rejected the event"
    db.commit()
    return {"ok": True, "queue_entry_id": entry.id, "status": entry.status}


@router.get("/api/call-results/retry-queue/tomoru-events/status")
def tomoru_events_status(db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    portal_id = resolve_portal_id(settings)
    state = TomoruIntegrationStore(db, portal_id).load()
    return {
        "mode": "live" if settings.tomoru_events_enabled else "dry_run",
        "external_calls_enabled": settings.tomoru_events_enabled,
        "bot_id_configured": bool(settings.tomoru_bot_id),
        "public_base_url_configured": bool(settings.tomoru_public_base_url),
        "webhook_secret_configured": bool(settings.tomoru_webhook_secret),
        "callback_configured": bool(state and state.callback_url),
        "callback_host": urlparse(state.callback_url).hostname if state else None,
    }


@router.get("/api/call-results/retry-queue/tomoru-api/status")
def tomoru_api_status(db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    return {
        "mode": "enabled" if settings.tomoru_batch_creation_enabled else "dry_run",
        "api_base_url": settings.tomoru_api_base_url,
        "api_key_configured": bool(settings.tomoru_api_key),
        "agent_id_configured": bool(settings.tomoru_agent_id),
        "batch_creation_enabled": settings.tomoru_batch_creation_enabled,
        "batch_auto_launch_enabled": settings.tomoru_batch_auto_launch_enabled,
        "result_callback_configured": bool(settings.tomoru_result_callback_url),
    }


@router.post("/api/call-results/retry-queue/tomoru-batches/dispatch")
def dispatch_tomoru_batches(
    body: TomoruBatchDispatchRequest,
    db: Session = Depends(get_session),
    _admin=Depends(require_role("admin")),
):
    settings = get_app_settings(db)
    portal_id = resolve_portal_id(settings)
    entries = RetryQueueGateway(db, portal_id).list_entries(
        import_id=body.import_id,
        limit=None,
    )
    if body.entry_ids:
        allowed_ids = set(body.entry_ids)
        entries = [entry for entry in entries if entry.id in allowed_ids]
    entries = entries[: body.limit]
    report = TomoruBatchDispatcher(settings).dispatch(
        entries,
        dry_run=body.dry_run,
        launch=body.launch,
    )
    db.commit()
    return report


@router.post("/api/call-results/retry-queue/tomoru-events/dispatch")
def dispatch_tomoru_events(
    body: TomoruDispatchRequest,
    db: Session = Depends(get_session),
):
    settings = get_app_settings(db)
    portal_id = resolve_portal_id(settings)
    entries = RetryQueueGateway(db, portal_id).list_entries(
        import_id=body.import_id,
        limit=None,
    )
    if body.entry_ids:
        allowed_ids = set(body.entry_ids)
        entries = [entry for entry in entries if entry.id in allowed_ids]
    entries = entries[: body.limit]
    state = TomoruIntegrationStore(db, portal_id).load()
    report = TomoruEventDispatcher(settings).dispatch(
        entries,
        callback_url=state.callback_url if state else None,
        dry_run=body.dry_run,
        include_future=body.include_future,
    )
    db.commit()
    report["callback_configured"] = bool(state and state.callback_url)
    report["bot_id_configured"] = bool(settings.tomoru_bot_id)
    return report
