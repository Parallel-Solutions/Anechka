"""Idempotency key tests."""

from datetime import datetime, timezone

from app.services.call_results.idempotency import build_retry_idempotency_key


def test_retry_idempotency_key_format():
    key = build_retry_idempotency_key(
        portal_id="example.bitrix24.ru",
        deal_id=100,
        contact_id=55,
        phone=None,
        source_call_id="call-1",
        reason="callback_later",
        callback_at=datetime(2026, 7, 1, 15, 0, tzinfo=timezone.utc),
    )
    assert key.startswith("example.bitrix24.ru:100:55:call-1:callback_later:")
    assert "2026-07-01" in key


def test_retry_key_uses_phone_when_no_contact():
    key = build_retry_idempotency_key(
        portal_id="p",
        deal_id=1,
        contact_id=None,
        phone="9161234567",
        source_call_id=None,
        reason="alternate_contact",
        callback_at=None,
    )
    assert ":9161234567:" in key
