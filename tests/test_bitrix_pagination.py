"""Tests for Bitrix client pagination and retry."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from app.config import Settings
from app.exceptions import BitrixAPIError, BitrixRateLimitError
from app.services.bitrix_client import BitrixClient


def _settings():
    return Settings(
        bitrix_webhook_url="https://example.bitrix24.ru/rest/1/token",
        max_retries=2,
        retry_base_delay=0.01,
    )


def test_pagination_next():
    client = BitrixClient(_settings())
    responses = [
        {"result": [{"ID": "1"}], "next": 50},
        {"result": [{"ID": "2"}]},
    ]

    with patch.object(client, "call", side_effect=responses):
        items = client.get_paginated("crm.deal.list", {"filter": {}}, limit=None)
    assert len(items) == 2


def test_http_429_retry():
    client = BitrixClient(_settings())

    ok_response = MagicMock()
    ok_response.status_code = 200
    ok_response.json.return_value = {"result": {"ID": 1}}

    rate_response = MagicMock()
    rate_response.status_code = 429
    rate_response.json.return_value = {"error": "QUERY_LIMIT_EXCEEDED"}

    with patch.object(client.session, "post", side_effect=[rate_response, ok_response]):
        data = client.call("profile")
    assert "result" in data


def test_json_error_raises():
    client = BitrixClient(_settings())
    bad = MagicMock()
    bad.status_code = 200
    bad.json.return_value = {"error": "INVALID_REQUEST", "error_description": "Bad"}
    bad.raise_for_status = MagicMock()

    with patch.object(client.session, "post", return_value=bad):
        with pytest.raises(BitrixAPIError):
            client.call("crm.deal.list")


def test_http_400_not_found_no_retry():
    client = BitrixClient(_settings())
    not_found = MagicMock()
    not_found.status_code = 400
    not_found.json.return_value = {
        "error": "NOT_FOUND",
        "error_description": "Элемент не найден",
    }
    not_found.raise_for_status = MagicMock(
        side_effect=requests.HTTPError(response=not_found)
    )

    with patch.object(client.session, "post", return_value=not_found) as post_mock:
        with patch("app.services.bitrix_client.time.sleep") as sleep_mock:
            with pytest.raises(BitrixAPIError, match="Элемент не найден"):
                client.call("crm.item.get")
    assert post_mock.call_count == 1
    sleep_mock.assert_not_called()
