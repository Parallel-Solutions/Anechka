"""Focused tests for initial export campaigns in Tomoru."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.config import get_settings
from app.services.call_results.tomoru_api import (
    TomoruApiClient,
    TomoruInitialBatchDispatcher,
)
from app.services.call_results.tomoru_initial_campaign import TomoruInitialCampaignPlanner


def _row(phone: str, timezone_name: str, deal_id: int):
    return SimpleNamespace(
        phone=phone,
        timezone=timezone_name,
        deal_id=deal_id,
        contact_id=deal_id + 100,
        fio=f"Контакт {deal_id}",
        company="Компания",
        deal_title=f"Сделка {deal_id}",
    )


def _settings(**updates):
    values = {
        "tomoru_api_base_url": "https://app.tomoru.test",
        "tomoru_api_key": "test-key",
        "tomoru_agent_id": "agent-1",
        "tomoru_batch_creation_enabled": True,
        "tomoru_batch_auto_launch_enabled": False,
        "tomoru_batch_max_retries": 3,
        "tomoru_batch_retry_delay_seconds": 300,
    }
    values.update(updates)
    return get_settings().model_copy(update=values)


def test_planner_groups_timezones_for_next_ten_am_local():
    now = datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc)
    drafts = TomoruInitialCampaignPlanner().plan(
        [
            _row("79161111111", "Europe/Moscow", 1),
            _row("79212222222", "Asia/Tomsk", 2),
            _row("79161111111", "Europe/Moscow", 3),
        ],
        now=now,
    )

    assert len(drafts) == 2
    by_timezone = {draft.timezone: draft for draft in drafts}
    assert by_timezone["Europe/Moscow"].scheduled_at == datetime(
        2026, 7, 24, 7, 0, tzinfo=timezone.utc
    )
    assert by_timezone["Asia/Tomsk"].scheduled_at == datetime(
        2026, 7, 25, 3, 0, tzinfo=timezone.utc
    )
    assert len(by_timezone["Europe/Moscow"].rows) == 1


def test_dispatcher_creates_pending_batches_without_launch():
    calls = []

    class Response:
        status_code = 201
        text = "ok"

        def __init__(self, batch_id):
            self.batch_id = batch_id

        def json(self):
            return {"id": self.batch_id, "status": "pending"}

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response(f"batch-{len(calls)}")

    settings = _settings()
    dispatcher = TomoruInitialBatchDispatcher(
        settings,
        client=TomoruApiClient(settings, request=fake_request),
    )
    report = dispatcher.dispatch(
        [_row("79298695656", "Europe/Moscow", 462)],
        now=datetime(2026, 7, 24, 4, 0, tzinfo=timezone.utc),
    )

    assert report["created_batches"] == 1
    assert report["launched_batches"] == 0
    assert calls[0][0:2] == ("POST", "https://app.tomoru.test/api/call_batches")
    csv_payload = calls[0][2]["files"]["csv_file"][1].decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(csv_payload)))
    assert rows[0]["phone_number"] == "+79298695656"
    assert rows[0]["deal_id"] == "462"
    assert rows[0]["timezone"] == "Europe/Moscow"

def test_initial_campaign_route_uses_current_export_selection(client):
    row = _row("79298695656", "Europe/Moscow", 462)
    report = {
        "mode": "live",
        "external_calls": True,
        "prepared_batches": 1,
        "prepared_contacts": 1,
        "created_batches": 1,
        "launched_batches": 0,
        "failed_batches": 0,
        "batches": [],
    }
    with (
        patch("app.routers.exports.LprTomoruService") as service_cls,
        patch("app.routers.exports.TomoruInitialBatchDispatcher") as dispatcher_cls,
        patch("app.routers.exports.load_lpr_config"),
        patch("app.routers.exports.resolve_portal_id", return_value="test.portal"),
    ):
        service_cls.return_value.report_rows = [row]
        service_cls.return_value.run_lpr_tomoru_export.return_value = "unused.csv"
        dispatcher_cls.return_value.dispatch.return_value = report
        response = client.post(
            "/exports/tomoru/campaigns",
            json={
                "entity_type": "deal",
                "category_id": 15,
                "stage_ids": [],
                "region_ids": [],
                "local_call_time": "10:00",
            },
        )

    assert response.status_code == 200
    assert response.json()["created_batches"] == 1
    params = service_cls.return_value.run_lpr_tomoru_export.call_args.args[0]
    assert params["region_ids"] == []
    assert params["group_by_timezone"] is False
    dispatcher_cls.return_value.dispatch.assert_called_once()
