"""Tests for settings save and merge priority."""

from __future__ import annotations

from app.dependencies import get_app_settings
from app.services.security_service import mask_webhook


def test_save_webhook_overrides_env(client, db_session):
    new_url = "https://newcompany.bitrix24.ru/rest/99/newtoken123"
    expected_masked = mask_webhook(new_url)

    resp = client.post(
        "/settings",
        data={
            "bitrix_webhook_url": new_url,
            "connect_timeout": 10.0,
            "read_timeout": 60.0,
            "max_retries": 5,
            "retry_base_delay": 1.0,
            "max_export_size": 5000,
            "export_dir": "./exports",
            "log_level": "INFO",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "webhook=updated" in resp.headers["location"]

    settings = get_app_settings(db_session)
    assert settings.bitrix_webhook_url == new_url

    page = client.get("/settings")
    assert page.status_code == 200
    assert expected_masked in page.text
    assert mask_webhook("https://example.bitrix24.ru/rest/1/token") not in page.text


def test_save_settings_keeps_webhook_when_field_empty(client, db_session):
    initial_url = "https://initial.bitrix24.ru/rest/1/abc123"
    client.post(
        "/settings",
        data={
            "bitrix_webhook_url": initial_url,
            "connect_timeout": 10.0,
            "read_timeout": 60.0,
            "max_retries": 5,
            "retry_base_delay": 1.0,
            "max_export_size": 5000,
            "export_dir": "./exports",
            "log_level": "INFO",
        },
        follow_redirects=True,
    )

    resp = client.post(
        "/settings",
        data={
            "bitrix_webhook_url": "",
            "connect_timeout": 15.0,
            "read_timeout": 60.0,
            "max_retries": 5,
            "retry_base_delay": 1.0,
            "max_export_size": 5000,
            "export_dir": "./exports",
            "log_level": "INFO",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "webhook=unchanged" in resp.headers["location"]

    settings = get_app_settings(db_session)
    assert settings.bitrix_webhook_url == initial_url
    assert settings.connect_timeout == 15.0
