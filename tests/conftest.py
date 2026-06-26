"""Pytest configuration."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["BITRIX_WEBHOOK_URL"] = "https://example.bitrix24.ru/rest/1/token"
os.environ.setdefault("EXPORT_DIR", str(ROOT / "test_exports"))
os.environ["BASIC_AUTH_PASSWORD"] = ""

from app.config import get_settings

get_settings.cache_clear()

import app.database as database_module

database_module.engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
database_module.SessionLocal = sessionmaker(bind=database_module.engine, autocommit=False, autoflush=False)

from app.database import Base, get_db

import app.main as main_module

importlib.reload(main_module)


def _reload_app_with_auth_disabled() -> None:
    os.environ["BASIC_AUTH_PASSWORD"] = ""
    get_settings.cache_clear()
    importlib.reload(main_module)


@pytest.fixture(autouse=True)
def _disable_basic_auth_for_tests():
    _reload_app_with_auth_disabled()
    yield
    _reload_app_with_auth_disabled()


@pytest.fixture()
def db_session():
    Base.metadata.create_all(bind=database_module.engine)
    session = database_module.SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=database_module.engine)


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    main_module.app.dependency_overrides[get_db] = override_get_db
    with TestClient(main_module.app, raise_server_exceptions=True) as c:
        yield c
    main_module.app.dependency_overrides.clear()
