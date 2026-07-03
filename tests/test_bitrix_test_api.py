"""Tests for Bitrix manual test API on settings page."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.call_results.fake_bitrix_gateway import FakeBitrixGateway


@pytest.fixture()
def fake_gateway():
    return FakeBitrixGateway()


def _mock_deal_get(client_mock, deal_id: int = 123, assigned_by_id: int = 42):
    client_mock.call.return_value = {"result": {"ID": str(deal_id), "ASSIGNED_BY_ID": assigned_by_id}}


def test_bitrix_test_comment_success(client, db_session, fake_gateway):
    with patch("app.services.bitrix_test_service.BitrixClient") as client_cls:
        client_mock = MagicMock()
        client_cls.return_value = client_mock
        _mock_deal_get(client_mock)
        with patch("app.services.bitrix_test_service.build_bitrix_gateway", return_value=fake_gateway):
            resp = client.post("/api/bitrix/test/comment", json={"deal_id": 123})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "Комментарий добавлен" in data["message"]
    assert data["external_id"] == "1"
    assert len(fake_gateway.comments) == 1
    assert fake_gateway.comments[0]["fields"]["ENTITY_ID"] == 123


def test_bitrix_test_todo_success(client, db_session, fake_gateway):
    with patch("app.services.bitrix_test_service.BitrixClient") as client_cls:
        client_mock = MagicMock()
        client_cls.return_value = client_mock
        _mock_deal_get(client_mock)
        with patch("app.services.bitrix_test_service.build_bitrix_gateway", return_value=fake_gateway):
            resp = client.post("/api/bitrix/test/todo", json={"deal_id": 123})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "CRM-дело создано" in data["message"]
    assert len(fake_gateway.todos) == 1
    assert fake_gateway.todos[0]["ownerId"] == 123
    assert fake_gateway.todos[0]["responsibleId"] == 42


def test_bitrix_test_contact_success(client, db_session, fake_gateway):
    with patch("app.services.bitrix_test_service.BitrixClient") as client_cls:
        client_mock = MagicMock()
        client_cls.return_value = client_mock
        _mock_deal_get(client_mock)
        with patch("app.services.bitrix_test_service.build_bitrix_gateway", return_value=fake_gateway):
            resp = client.post("/api/bitrix/test/contact", json={"deal_id": 123})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "привязан" in data["message"]
    assert data["external_id"]
    assert 123 in fake_gateway.deal_links
    assert int(data["external_id"]) in fake_gateway.deal_links[123]


def test_bitrix_test_deal_not_found(client, db_session, fake_gateway):
    with patch("app.services.bitrix_test_service.BitrixClient") as client_cls:
        client_mock = MagicMock()
        client_cls.return_value = client_mock
        client_mock.call.return_value = {"result": None}
        with patch("app.services.bitrix_test_service.build_bitrix_gateway", return_value=fake_gateway):
            resp = client.post("/api/bitrix/test/comment", json={"deal_id": 999})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "не найдена" in data["message"]


def test_bitrix_test_no_webhook(client, db_session):
    from app.config import Settings, get_settings

    empty = Settings.model_construct(**{**get_settings().model_dump(), "bitrix_webhook_url": ""})

    with patch("app.routers.settings.get_app_settings", return_value=empty):
        resp = client.post("/api/bitrix/test/comment", json={"deal_id": 123})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "вебхука" in data["message"]


@pytest.mark.parametrize("deal_id", [0, -1])
def test_bitrix_test_invalid_deal_id(client, deal_id):
    resp = client.post("/api/bitrix/test/comment", json={"deal_id": deal_id})
    assert resp.status_code == 422


def test_bitrix_test_gateway_failure(client, db_session):
    gateway = FakeBitrixGateway(fail_on={"crm.timeline.comment.add"})
    with patch("app.services.bitrix_test_service.BitrixClient") as client_cls:
        client_mock = MagicMock()
        client_cls.return_value = client_mock
        _mock_deal_get(client_mock)
        with patch("app.services.bitrix_test_service.build_bitrix_gateway", return_value=gateway):
            resp = client.post("/api/bitrix/test/comment", json={"deal_id": 123})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "комментарий" in data["message"].lower() or "failure" in data["message"].lower()


def test_settings_page_has_bitrix_test_form(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "Тест запросов в Bitrix" in resp.text
    assert 'id="bitrix-test-deal-id"' in resp.text
    assert 'id="btn-bitrix-test-comment"' in resp.text
