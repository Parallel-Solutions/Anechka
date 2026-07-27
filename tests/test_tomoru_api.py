"""Tests for the current Tomoru REST API batch integration."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models import CallRetryQueueEntry
from app.services.auth_service import resolve_portal_id
from app.services.call_results.tomoru_api import (
    TomoruApiClient,
    TomoruBatchDispatcher,
)


def _settings(**updates):
    defaults = {
        "tomoru_api_base_url": "https://app.tomoru.test",
        "tomoru_api_key": "test-key",
        "tomoru_agent_id": "b418f3f0-3ca8-41ef-98c1-f9ff5b1f1aa0",
        "tomoru_batch_creation_enabled": False,
        "tomoru_batch_auto_launch_enabled": False,
        "tomoru_batch_max_retries": 3,
        "tomoru_batch_retry_delay_seconds": 300,
    }
    defaults.update(updates)
    return get_settings().model_copy(update=defaults)


def _entry(*, entry_id: int = 1, callback_at: datetime | None = None):
    return CallRetryQueueEntry(
        id=entry_id,
        portal_id="test",
        phone_normalized="79298695656",
        phone_extension="321",
        callback_at=callback_at,
        callback_text="Перезвонить после обеда",
        reason="callback_later",
        status="scheduled" if callback_at else "ready",
        attempt_count=0,
        search_required=False,
        idempotency_key=f"retry-{entry_id}",
        timezone="Europe/Moscow",
        deal_id=1001,
        contact_id=2001,
    )


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def test_client_lists_agents_with_bearer_key():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse({"data": []})

    client = TomoruApiClient(_settings(), request=fake_request)

    assert client.list_agents() == {"data": []}
    assert calls[0][0:2] == ("GET", "https://app.tomoru.test/api/agents")
    assert calls[0][2]["headers"]["Authorization"] == "Bearer test-key"


def test_client_creates_scheduled_batch_with_csv():
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse({"id": "batch-1", "status": "pending"}, status_code=201)

    now = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
    entry = _entry(callback_at=now + timedelta(hours=2))
    settings = _settings()
    dispatcher = TomoruBatchDispatcher(
        settings,
        client=TomoruApiClient(settings, request=fake_request),
    )

    report = dispatcher.dispatch(
        [entry],
        dry_run=False,
        launch=False,
        now=now,
    )

    assert report["mode"] == "blocked"
    assert calls == []

    settings = settings.model_copy(update={"tomoru_batch_creation_enabled": True})
    dispatcher = TomoruBatchDispatcher(
        settings,
        client=TomoruApiClient(settings, request=fake_request),
    )
    report = dispatcher.dispatch(
        [entry],
        dry_run=False,
        launch=False,
        now=now,
    )

    assert report["created_batches"] == 1
    assert report["launched_batches"] == 0
    assert entry.status == "scheduled"
    assert entry.dispatched_campaign_id == "batch-1"
    method, url, kwargs = calls[0]
    assert (method, url) == ("POST", "https://app.tomoru.test/api/call_batches")
    assert kwargs["data"]["agent_id"] == settings.tomoru_agent_id
    assert kwargs["data"]["start_at"] == "2026-07-24T11:00:00Z"
    csv_payload = kwargs["files"]["csv_file"][1].decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_payload)))
    assert rows == [
        {
            "phone_number": "+79298695656",
            "queue_entry_id": "1",
            "reason": "callback_later",
            "timezone": "Europe/Moscow",
            "deal_id": "1001",
            "contact_id": "2001",
            "callback_text": "Перезвонить после обеда",
            "phone_extension": "321",
        }
    ]


def test_dry_run_never_calls_tomoru():
    class ForbiddenClient:
        def create_batch(self, draft):
            raise AssertionError("Tomoru must not be called")

        def launch_batch(self, batch_id):
            raise AssertionError("Tomoru must not be called")

    now = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)
    entry = _entry(callback_at=now + timedelta(days=90))
    report = TomoruBatchDispatcher(
        _settings(),
        client=ForbiddenClient(),
    ).dispatch([entry], now=now)

    assert report["mode"] == "dry_run"
    assert report["external_calls"] is False
    assert report["prepared_batches"] == 1
    assert report["prepared_contacts"] == 1
    assert entry.dispatched_campaign_id is None


def test_launch_requires_separate_feature_flag():
    class ForbiddenClient:
        def create_batch(self, draft):
            raise AssertionError("Batch must not be created")

        def launch_batch(self, batch_id):
            raise AssertionError("Batch must not be launched")

    report = TomoruBatchDispatcher(
        _settings(tomoru_batch_creation_enabled=True),
        client=ForbiddenClient(),
    ).dispatch([_entry()], dry_run=False, launch=True)

    assert report["mode"] == "blocked"
    assert report["blocked_reason"] == "TOMORU_BATCH_AUTO_LAUNCH_ENABLED=false"


def test_explicit_launch_updates_queue_status():
    calls = []

    class FakeClient:
        def create_batch(self, draft):
            calls.append(("create", draft.campaign_name))
            return {"id": "batch-live"}

        def launch_batch(self, batch_id):
            calls.append(("launch", batch_id))
            return {"data": {"id": batch_id}}

    entry = _entry()
    report = TomoruBatchDispatcher(
        _settings(
            tomoru_batch_creation_enabled=True,
            tomoru_batch_auto_launch_enabled=True,
        ),
        client=FakeClient(),
    ).dispatch([entry], dry_run=False, launch=True)

    assert report["created_batches"] == 1
    assert report["launched_batches"] == 1
    assert calls == [
        ("create", report["batches"][0]["campaign_name"]),
        ("launch", "batch-live"),
    ]
    assert entry.status == "sent_to_tomoru"


def test_batch_dispatch_endpoint_defaults_to_dry_run(client, db_session, monkeypatch):
    from app.routers import tomoru_integration

    settings = _settings()
    monkeypatch.setattr(tomoru_integration, "get_app_settings", lambda db: settings)
    entry = _entry(entry_id=50)
    entry.portal_id = resolve_portal_id(settings)
    db_session.add(entry)
    db_session.commit()

    response = client.post(
        "/api/call-results/retry-queue/tomoru-batches/dispatch",
        json={"entry_ids": [entry.id]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "dry_run"
    assert data["external_calls"] is False
    assert data["prepared_contacts"] == 1


def test_batch_dispatch_endpoint_empty_ids_dispatches_nothing(client, db_session, monkeypatch):
    from app.routers import tomoru_integration

    settings = _settings()
    monkeypatch.setattr(tomoru_integration, "get_app_settings", lambda db: settings)
    entry = _entry(entry_id=51)
    entry.portal_id = resolve_portal_id(settings)
    db_session.add(entry)
    db_session.commit()

    response = client.post(
        "/api/call-results/retry-queue/tomoru-batches/dispatch",
        json={"entry_ids": []},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["prepared_contacts"] == 0
    assert data["prepared_batches"] == 0
