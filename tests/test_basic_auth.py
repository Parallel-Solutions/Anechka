"""Tests for HTTP Basic Auth middleware."""

from __future__ import annotations

import base64
import importlib
import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings


def _auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def auth_client(db_session):
    os.environ["BASIC_AUTH_USERNAME"] = "testuser"
    os.environ["BASIC_AUTH_PASSWORD"] = "testpass"
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

    os.environ["BASIC_AUTH_USERNAME"] = "admin"
    os.environ["BASIC_AUTH_PASSWORD"] = ""
    get_settings.cache_clear()
    importlib.reload(main_module)


def test_health_without_auth(auth_client):
    resp = auth_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_root_requires_auth(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate", "").startswith("Basic")


def test_root_with_valid_auth(auth_client):
    resp = auth_client.get("/", headers=_auth_header("testuser", "testpass"))
    assert resp.status_code == 200


def test_root_with_invalid_auth(auth_client):
    resp = auth_client.get("/", headers=_auth_header("testuser", "wrong"))
    assert resp.status_code == 401
