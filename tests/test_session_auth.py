"""Tests for session authentication middleware."""

from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


@pytest.fixture()
def secured_client(db_session):
    os.environ["APP_AUTH_DISABLED"] = "0"
    get_settings.cache_clear()

    from app.database import get_db
    import app.main as main_module

    importlib.reload(main_module)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app, raise_server_exceptions=True) as client:
        yield client
    main_module.app.dependency_overrides.clear()

    os.environ["APP_AUTH_DISABLED"] = "1"
    get_settings.cache_clear()
    importlib.reload(main_module)


def test_health_without_auth(secured_client):
    resp = secured_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_root_redirects_to_login(secured_client):
    resp = secured_client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("/login")


def test_api_requires_auth(secured_client):
    resp = secured_client.get("/api/intelligent-export/conversations")
    assert resp.status_code == 401


def test_login_and_access(secured_client, db_session):
    from app.services.auth_service import AuthService

    auth = AuthService(get_settings(), db_session)
    auth.create_user("secure@example.com", "secret12", display_name="Secure")

    login = secured_client.post(
        "/auth/login",
        json={"email": "secure@example.com", "password": "secret12"},
    )
    assert login.status_code == 200

    resp = secured_client.get("/")
    assert resp.status_code == 200

    me = secured_client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "secure@example.com"


def test_logout_clears_session(secured_client, db_session):
    from app.services.auth_service import AuthService

    auth = AuthService(get_settings(), db_session)
    auth.create_user("logout@example.com", "secret12")

    secured_client.post("/auth/login", json={"email": "logout@example.com", "password": "secret12"})
    assert secured_client.get("/auth/me").status_code == 200

    secured_client.post("/auth/logout")
    assert secured_client.get("/auth/me").status_code == 401
