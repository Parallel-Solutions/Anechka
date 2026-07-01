"""Tests for Bitrix CRM import queue and scheduler."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.models import utcnow
from app.repositories.sync_repository import SyncRepository
from app.services.bitrix_import.import_queue_service import (
    ConcurrentImportError,
    FullImportNotConfirmedError,
    enqueue_import,
    resolve_import_mode,
)
from app.services.bitrix_import.scheduler_service import BitrixImportScheduler

PORTAL = "example.bitrix24.ru"


@pytest.fixture()
def sync_repo(db_session):
    return SyncRepository(db_session)


def _settings(**kwargs) -> Settings:
    defaults = {
        "bitrix_webhook_url": "https://example.bitrix24.ru/rest/1/token",
        "bitrix_import_schedule_enabled": True,
        "bitrix_import_schedule_interval_minutes": 60,
    }
    defaults.update(kwargs)
    return Settings.model_construct(**defaults)


def _complete_run(sync_repo: SyncRepository, portal: str, mode: str, *, hours_ago: float = 2) -> None:
    run = sync_repo.create_run(portal, mode, statistics={"analyze_metadata": True})
    run.status = "completed"
    run.finished_at = utcnow() - timedelta(hours=hours_ago)
    sync_repo.db.commit()


def test_resolve_import_mode_full_without_checkpoint(db_session, sync_repo):
    assert resolve_import_mode(sync_repo, PORTAL, "incremental") == "full"


def test_resolve_import_mode_incremental_with_checkpoint(db_session, sync_repo):
    sync_repo.upsert_checkpoint(
        PORTAL,
        "entities",
        1,
        cursor_time=utcnow(),
        cursor_id=100,
    )
    assert resolve_import_mode(sync_repo, PORTAL, "incremental") == "incremental"


def test_enqueue_import_raises_on_active_run(db_session, sync_repo):
    sync_repo.create_run(PORTAL, "incremental")
    db_session.commit()
    with pytest.raises(ConcurrentImportError):
        enqueue_import(sync_repo, PORTAL, mode="incremental")


def test_enqueue_import_raises_full_without_confirm(db_session, sync_repo):
    with pytest.raises(FullImportNotConfirmedError):
        enqueue_import(sync_repo, PORTAL, mode="full", confirm_full=False)


def test_enqueue_import_scheduler_fields(db_session, sync_repo):
    sync_repo.upsert_checkpoint(PORTAL, "entities", 1, cursor_time=utcnow(), cursor_id=1)
    run = enqueue_import(
        sync_repo,
        PORTAL,
        mode="incremental",
        requested_by="scheduler",
        analyze_metadata=True,
    )
    assert run.mode == "incremental"
    assert run.requested_by == "scheduler"
    assert run.statistics_json == {"analyze_metadata": True}


def test_scheduler_skips_when_interval_not_elapsed(db_session, sync_repo):
    _complete_run(sync_repo, PORTAL, "incremental", hours_ago=0.5)
    scheduler = BitrixImportScheduler()
    assert scheduler.maybe_enqueue(db_session, _settings(), PORTAL) is False


def test_scheduler_enqueues_when_interval_elapsed(db_session, sync_repo):
    sync_repo.upsert_checkpoint(PORTAL, "entities", 1, cursor_time=utcnow(), cursor_id=1)
    _complete_run(sync_repo, PORTAL, "incremental", hours_ago=2)
    scheduler = BitrixImportScheduler()
    assert scheduler.maybe_enqueue(db_session, _settings(), PORTAL) is True
    runs = sync_repo.list_runs(PORTAL, limit=1)
    assert runs[0].requested_by == "scheduler"
    assert runs[0].status == "pending"
    assert runs[0].mode == "incremental"


def test_scheduler_skips_when_disabled(db_session, sync_repo):
    _complete_run(sync_repo, PORTAL, "incremental", hours_ago=2)
    scheduler = BitrixImportScheduler()
    assert scheduler.maybe_enqueue(db_session, _settings(bitrix_import_schedule_enabled=False), PORTAL) is False


def test_scheduler_skips_without_webhook(db_session, sync_repo):
    _complete_run(sync_repo, PORTAL, "incremental", hours_ago=2)
    scheduler = BitrixImportScheduler()
    assert scheduler.maybe_enqueue(db_session, _settings(bitrix_webhook_url=""), PORTAL) is False


def test_scheduler_skips_on_active_run(db_session, sync_repo):
    _complete_run(sync_repo, PORTAL, "incremental", hours_ago=2)
    sync_repo.create_run(PORTAL, "incremental")
    db_session.commit()
    scheduler = BitrixImportScheduler()
    assert scheduler.maybe_enqueue(db_session, _settings(), PORTAL) is False


def test_scheduler_fallback_to_full_without_checkpoint(db_session, sync_repo):
    _complete_run(sync_repo, PORTAL, "full", hours_ago=2)
    scheduler = BitrixImportScheduler()
    assert scheduler.maybe_enqueue(db_session, _settings(), PORTAL) is True
    runs = sync_repo.list_runs(PORTAL, limit=1)
    assert runs[0].mode == "full"
    assert runs[0].requested_by == "scheduler"


def test_create_import_api_still_blocks_concurrent(db_session, sync_repo, client: TestClient, monkeypatch):
    sync_repo.create_run(PORTAL, "incremental")
    db_session.commit()
    monkeypatch.setattr("app.routers.admin_bitrix._portal", lambda _db: PORTAL)
    resp = client.post("/admin/bitrix/imports", json={"mode": "incremental"})
    assert resp.status_code == 409
