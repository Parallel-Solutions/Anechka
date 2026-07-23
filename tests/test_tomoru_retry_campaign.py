"""Dry-run Tomoru campaign planning tests."""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.models import CallRetryQueueEntry
from app.services.auth_service import resolve_portal_id
from app.services.call_results.tomoru_retry_campaign import TomoruRetryCampaignPlanner


def _entry(
    entry_id: int,
    *,
    timezone_name: str,
    callback_at: datetime | None = None,
    phone_extension: str | None = None,
) -> CallRetryQueueEntry:
    return CallRetryQueueEntry(
        id=entry_id,
        portal_id="test",
        phone_normalized=f"7929000000{entry_id}",
        phone_extension=phone_extension,
        callback_at=callback_at,
        reason="callback_later",
        status="scheduled" if callback_at else "ready",
        search_required=False,
        idempotency_key=f"queue-{entry_id}",
        timezone=timezone_name,
    )


def test_planner_groups_by_timezone_and_preserves_extension():
    now = datetime(2026, 7, 23, 8, 0, tzinfo=timezone.utc)
    entries = [
        _entry(1, timezone_name="Europe/Moscow", phone_extension="321"),
        _entry(
            2,
            timezone_name="Europe/Moscow",
            callback_at=datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc),
        ),
        _entry(3, timezone_name="Asia/Tomsk"),
    ]
    planner = TomoruRetryCampaignPlanner("10:00")

    first = planner.plan(entries, now=now)
    second = planner.plan(entries, now=now)

    assert len(first) == 2
    assert [draft.idempotency_key for draft in first] == [
        draft.idempotency_key for draft in second
    ]
    moscow = next(draft for draft in first if draft.timezone == "Europe/Moscow")
    tomsk = next(draft for draft in first if draft.timezone == "Asia/Tomsk")
    assert moscow.scheduled_at.isoformat() == "2026-07-24T10:00:00+03:00"
    assert len(moscow.contacts) == 2
    assert moscow.contacts[0].phone_extension == "321"
    assert tomsk.scheduled_at.isoformat() == "2026-07-24T10:00:00+07:00"


def test_preview_endpoint_is_explicitly_dry_run(client, db_session):
    portal_id = resolve_portal_id(get_settings())
    db_session.add(
        CallRetryQueueEntry(
            portal_id=portal_id,
            phone_normalized="79298695656",
            phone_extension="123",
            reason="no_answer",
            status="ready",
            search_required=False,
            idempotency_key="preview-only-entry",
            timezone="Europe/Moscow",
        )
    )
    db_session.commit()

    response = client.get("/api/call-results/retry-queue/tomoru-preview")

    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "dry_run"
    assert data["external_calls"] is False
    assert data["campaign_count"] == 1
    assert data["campaigns"][0]["timezone"] == "Europe/Moscow"
    assert data["campaigns"][0]["contacts"][0]["phone_extension"] == "123"
