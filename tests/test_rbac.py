"""Role-based access checks for administrative routes and navigation."""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture()
def rbac_client(db_session):
    os.environ["APP_AUTH_DISABLED"] = "0"
    get_settings.cache_clear()

    from app.database import get_db
    import app.main as main_module

    importlib.reload(main_module)

    def override_get_db():
        yield db_session

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app, raise_server_exceptions=True) as client:
        yield client

    main_module.app.dependency_overrides.clear()
    os.environ["APP_AUTH_DISABLED"] = "1"
    get_settings.cache_clear()
    importlib.reload(main_module)


def _login_as(client: TestClient, db_session, role: str):
    from app.services.auth_service import AuthService

    email = f"{role}@example.com"
    AuthService(get_settings(), db_session).create_user(
        email,
        "secret12",
        role=role,
        display_name=role.title(),
    )
    response = client.post("/auth/login", json={"email": email, "password": "secret12"})
    assert response.status_code == 200
    return response.json()["user"]


@pytest.mark.parametrize("role", ["viewer", "analyst"])
def test_non_admin_cannot_access_administration(rbac_client, db_session, role):
    _login_as(rbac_client, db_session, role)

    protected_urls = [
        "/api/app-users",
        "/settings",
        "/settings/users",
        "/bitrix-import",
        "/admin/bitrix/imports",
        "/api/intelligent-export/audit",
        "/api/ai/lpr-config",
    ]
    for url in protected_urls:
        response = rbac_client.get(url)
        assert response.status_code == 403, url
        assert response.json()["detail"]["code"] == "ACCESS_DENIED"

    protected_mutations = [
        ("post", "/api/app-users", {"email": "blocked@example.com", "password": "secret12"}),
        ("post", "/settings", {}),
        ("post", "/admin/bitrix/imports", {"mode": "incremental"}),
        ("post", "/api/ai/prompts", {"title": "Blocked", "prompt": "Blocked"}),
    ]
    for method, url, body in protected_mutations:
        response = rbac_client.request(method, url, json=body)
        assert response.status_code == 403, url
        assert response.json()["detail"]["code"] == "ACCESS_DENIED"


def test_analyst_keeps_operational_access_and_admin_nav_is_hidden(rbac_client, db_session):
    user = _login_as(rbac_client, db_session, "analyst")

    assert user["role"] == "analyst"
    response = rbac_client.get("/exports")
    assert response.status_code == 200
    assert 'href="/settings"' not in response.text
    assert 'href="/settings/users"' not in response.text
    assert 'href="/bitrix-import"' not in response.text
    assert 'href="/exports"' in response.text


def test_admin_can_access_administration(rbac_client, db_session):
    user = _login_as(rbac_client, db_session, "admin")
    assert user["role"] == "admin"

    allowed_urls = [
        "/api/app-users",
        "/settings",
        "/settings/users",
        "/admin/bitrix/imports",
        "/api/intelligent-export/audit",
        "/api/ai/lpr-config",
    ]
    for url in allowed_urls:
        response = rbac_client.get(url)
        assert response.status_code == 200, url

    settings_page = rbac_client.get("/settings")
    assert 'href="/settings/users"' in settings_page.text
    assert 'href="/bitrix-import"' in settings_page.text


def test_admin_cannot_create_user_with_unknown_role(rbac_client, db_session):
    _login_as(rbac_client, db_session, "admin")

    response = rbac_client.post(
        "/api/app-users",
        json={
            "email": "invalid-role@example.com",
            "password": "secret12",
            "role": "owner",
        },
    )
    assert response.status_code == 422