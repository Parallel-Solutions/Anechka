"""Tests for the guarded Tomoru microservice event integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.models import CallRetryQueueEntry
from app.services.auth_service import resolve_portal_id
from app.services.call_results.tomoru_event_dispatcher import TomoruEventDispatcher


def _settings(**updates):
    return get_settings().model_copy(update=updates)


def _entry(*, entry_id: int = 1, callback_at: datetime | None = None):
    return CallRetryQueueEntry(
        id=entry_id,
        portal_id="test",
        phone_normalized="79298695656",
        phone_extension="321",
        callback_at=callback_at,
        callback_text="Call after lunch",
        reason="refusal_followup",
        status="scheduled" if callback_at else "ready",
        attempt_count=3,
        search_required=False,
        idempotency_key=f"tracking-{entry_id}",
        timezone="Europe/Moscow",
        deal_id=1001,
        contact_id=2001,
    )


def test_dry_run_prepares_tomoru_event_without_external_call():
    called = False

    def forbidden_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("HTTP must not be called in dry-run")

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    entry = _entry(callback_at=now + timedelta(days=90))
    dispatcher = TomoruEventDispatcher(
        _settings(tomoru_events_enabled=False, tomoru_bot_id=""),
        post=forbidden_post,
    )

    report = dispatcher.dispatch(
        [entry],
        callback_url=None,
        dry_run=True,
        include_future=True,
        now=now,
    )

    assert called is False
    assert report["mode"] == "dry_run"
    assert report["external_calls"] is False
    assert report["prepared"] == 1
    payload = report["events"][0]["payload"]
    assert payload["event"] == "coldCall"
    assert payload["trackingId"] == "tracking-1"
    assert payload["chatUri"] == "tel://+79298695656"
    assert payload["data"]["phoneExtension"] == "321"
    assert payload["data"]["attemptCount"] == 3
    assert entry.status == "scheduled"


def test_live_dispatch_is_blocked_while_feature_flag_is_off():
    called = False

    def forbidden_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("HTTP must stay blocked")

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    entry = _entry(callback_at=now - timedelta(minutes=1))
    dispatcher = TomoruEventDispatcher(
        _settings(tomoru_events_enabled=False, tomoru_bot_id="agent-id"),
        post=forbidden_post,
    )

    report = dispatcher.dispatch(
        [entry],
        callback_url="https://europe-west1-tomoru-2bb77.cloudfunctions.net/microserviceEvent/token",
        dry_run=False,
        now=now,
    )

    assert called is False
    assert report["mode"] == "blocked"
    assert report["blocked_reason"] == "TOMORU_EVENTS_ENABLED=false"
    assert entry.status == "scheduled"


def test_explicit_live_dispatch_updates_queue_with_fake_transport():
    calls = []

    class FakeResponse:
        status_code = 200
        text = "ok"

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    now = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)
    entry = _entry(callback_at=now - timedelta(minutes=1))
    dispatcher = TomoruEventDispatcher(
        _settings(tomoru_events_enabled=True, tomoru_bot_id="agent-id"),
        post=fake_post,
    )

    report = dispatcher.dispatch(
        [entry],
        callback_url="https://europe-west1-tomoru-2bb77.cloudfunctions.net/microserviceEvent/token",
        dry_run=False,
        now=now,
    )

    assert report["mode"] == "live"
    assert report["sent"] == 1
    assert report["failed"] == 0
    assert len(calls) == 1
    assert calls[0][1]["json"]["botId"] == "agent-id"
    assert entry.status == "sent_to_tomoru"
    assert entry.dispatched_campaign_id == "tracking-1"
    assert entry.dispatched_at is not None


def test_subscribe_and_status_callbacks_are_secret_protected(client, db_session, monkeypatch):
    from app.routers import tomoru_integration

    settings = _settings(
        tomoru_events_enabled=False,
        tomoru_bot_id="agent-id",
        tomoru_webhook_secret="test-secret",
    )
    monkeypatch.setattr(tomoru_integration, "get_app_settings", lambda db: settings)

    unauthorized = client.post(
        "/tomoru-hooks/subscribe",
        json={
            "tomoruCallbackUrl": "https://europe-west1-tomoru-2bb77.cloudfunctions.net/microserviceEvent/token"
        },
    )
    assert unauthorized.status_code == 401

    subscribed = client.post(
        "/tomoru-hooks/subscribe",
        headers={"X-Anechka-Tomoru-Secret": "test-secret"},
        json={
            "tomoruCallbackUrl": "https://europe-west1-tomoru-2bb77.cloudfunctions.net/microserviceEvent/token"
        },
    )
    assert subscribed.status_code == 200
    assert subscribed.json()["callback_host"] == "europe-west1-tomoru-2bb77.cloudfunctions.net"
    assert subscribed.json()["external_calls_enabled"] is False

    portal_id = resolve_portal_id(settings)
    entry = _entry(entry_id=10)
    entry.portal_id = portal_id
    db_session.add(entry)
    db_session.commit()

    completed = client.post(
        "/tomoru-hooks/event-status",
        headers={"X-Anechka-Tomoru-Secret": "test-secret"},
        json={
            "eventStatus": {
                "trackingId": "tracking-10",
                "event": "coldCall",
                "status": "processed",
            }
        },
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    db_session.refresh(entry)
    assert entry.status == "completed"


def test_dispatch_api_defaults_to_dry_run(client, db_session, monkeypatch):
    from app.routers import tomoru_integration

    settings = _settings(
        tomoru_events_enabled=False,
        tomoru_bot_id="",
        tomoru_webhook_secret="test-secret",
    )
    monkeypatch.setattr(tomoru_integration, "get_app_settings", lambda db: settings)
    portal_id = resolve_portal_id(settings)
    entry = _entry(entry_id=20)
    entry.portal_id = portal_id
    db_session.add(entry)
    db_session.commit()

    response = client.post(
        "/api/call-results/retry-queue/tomoru-events/dispatch",
        json={"entry_ids": [entry.id]},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "dry_run"
    assert data["external_calls"] is False
    assert data["prepared"] == 1
    assert data["events"][0]["payload"]["chatUri"] == "tel://+79298695656"


def test_openapi_template_is_available(client, db_session, monkeypatch):
    from app.routers import tomoru_integration

    settings = _settings(tomoru_public_base_url="https://anechka.test")
    monkeypatch.setattr(tomoru_integration, "get_app_settings", lambda db: settings)

    response = client.get("/tomoru-hooks/openapi.yaml")

    assert response.status_code == 200
    assert "tomoru/subscribe" in response.text
    assert "tomoru/event_status_changed" in response.text
    assert "https://anechka.test" in response.text
