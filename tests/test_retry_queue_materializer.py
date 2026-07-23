"""Safe local retry-queue materialization tests."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.config import get_settings
from app.models import (
    BitrixPreparedAction,
    CallResultImport,
    CallResultImportRow,
    CallRetryQueueEntry,
)
from app.services.auth_service import resolve_portal_id
from app.services.call_results.retry_queue_materializer import RetryQueueMaterializer


def _prepared_retry(db_session, *, reason: str = "callback_later") -> tuple[int, int]:
    portal_id = resolve_portal_id(get_settings())
    imp = CallResultImport(
        portal_id=portal_id,
        original_filename="safe-retry.csv",
        storage_key="call_results/safe-retry.csv",
        file_sha256="safe-retry-hash",
        file_size=1,
        status="ready",
    )
    db_session.add(imp)
    db_session.flush()
    row = CallResultImportRow(
        import_id=imp.id,
        source_row_number=2,
        raw_data={},
        normalized_data={},
        raw_phone="+7 (929) 869-56-56 доб. 321",
        normalized_phone="79298695656",
        phone_extension="321",
        match_status="matched",
        matched_deal_id=1001,
        business_signals={"callback_text": "Перезвонить в 10:00"},
        primary_outcome="callback_later",
        needs_manual_review=False,
        execution_status="prepared",
        llm_status="not_required",
        llm_required=False,
        manually_overridden=False,
        llm_input_truncated=False,
        is_duplicate=False,
        callback_at=datetime(2026, 7, 24, 10, 0, tzinfo=timezone.utc),
        campaign_id="source-campaign",
        attempts=3,
        call_id="source-call",
    )
    db_session.add(row)
    db_session.flush()
    action = BitrixPreparedAction(
        import_id=imp.id,
        import_row_id=row.id,
        action_group_id="safe-local-group",
        method="retry_queue.add",
        action_type="retry_queue_add",
        operation_type="retry_queue_add",
        payload={"reason": reason, "timezone": "Europe/Moscow"},
        human_summary="safe local retry",
        validation_status="valid",
        is_enabled=True,
        idempotency_key="safe-local-action",
        execution_status="prepared",
    )
    db_session.add(action)
    db_session.commit()
    return imp.id, row.id


def test_materializer_creates_local_queue_and_is_idempotent(db_session):
    import_id, _ = _prepared_retry(db_session)
    portal_id = resolve_portal_id(get_settings())
    service = RetryQueueMaterializer(db_session, portal_id)

    first = service.materialize_import(import_id)
    db_session.commit()
    second = service.materialize_import(import_id)
    db_session.commit()

    assert first.created == 1
    assert first.existing == 0
    assert second.created == 0
    assert second.existing == 1
    assert db_session.scalar(select(func.count()).select_from(CallRetryQueueEntry)) == 1
    entry = db_session.scalar(select(CallRetryQueueEntry))
    assert entry is not None
    assert entry.phone_normalized == "79298695656"
    assert entry.phone_extension == "321"
    assert entry.timezone == "Europe/Moscow"
    assert entry.status == "scheduled"
    assert entry.attempt_count == 3
    assert entry.campaign_id == "source-campaign"
    assert entry.dispatched_campaign_id is None


def test_materialize_api_and_csv_do_not_require_bitrix_execution(client, db_session):
    import_id, row_id = _prepared_retry(db_session)

    response = client.post(
        f"/api/call-results/imports/{import_id}/retry-queue/materialize",
        json={"row_ids": [row_id]},
    )

    assert response.status_code == 200
    assert response.json()["created"] == 1
    exported = client.get(f"/api/call-results/retry-queue/export.csv?import_id={import_id}")
    assert exported.status_code == 200
    assert exported.content.startswith(b"\xef\xbb\xbf")
    text = exported.content.decode("utf-8-sig")
    assert "phone_extension" in text
    assert "callback_at" in text
    assert "timezone" in text
    assert "79298695656" in text
    assert "321" in text


def test_import_page_has_local_only_retry_button(client, db_session):
    import_id, _ = _prepared_retry(db_session)

    response = client.get(f"/call-results/imports/{import_id}")

    assert response.status_code == 200
    assert 'id="btn-prepare-retry-export"' in response.text
    assert "без Bitrix" in response.text
