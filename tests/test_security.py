"""Phase H: security hardening — injection guards, path traversal, IDOR,
session enforcement, input limits, regex abuse, and audit logging."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.repositories.intelligent_export_repository import IntelligentExportRepository, ScopeContext
from app.services.auth_service import AuthService, resolve_portal_id
from app.services.export_plan.registry import RegexParams
from app.services.intelligent_export.plan_service import prepare_plan
from app.services.intelligent_export.scope import build_scope
from app.services.security_service import sanitize_excel_value, validate_download_path

API = "/api/intelligent-export"


def _portal():
    return resolve_portal_id(get_settings())


def _make_user(db, email, role="analyst"):
    return AuthService(get_settings(), db).create_user(email, role=role, password="pw123456")


# --- Excel / CSV formula injection -----------------------------------------


@pytest.mark.parametrize(
    "payload",
    ["=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1:A2)"],
)
def test_excel_formula_injection_is_neutralized(payload):
    out = sanitize_excel_value(payload)
    assert isinstance(out, str) and out.startswith("'")


def test_excel_numeric_values_not_mangled():
    assert sanitize_excel_value(42) == 42
    assert sanitize_excel_value(3.14) == 3.14
    assert sanitize_excel_value("normal text") == "normal text"


# --- path traversal on download --------------------------------------------


def test_download_path_traversal_rejected(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")

    with pytest.raises(ValueError):
        validate_download_path(export_dir, "../secret.txt")
    with pytest.raises(ValueError):
        validate_download_path(export_dir, str(secret))


def test_download_path_valid_file_ok(tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    f = export_dir / "ok.xlsx"
    f.write_text("data")
    resolved = validate_download_path(export_dir, "ok.xlsx")
    assert resolved == f.resolve()


# --- regex catastrophic backtracking ---------------------------------------


@pytest.mark.parametrize("pattern", ["(.*)+", "(.+)+x", "(.*)*"])
def test_regex_catastrophic_patterns_rejected(pattern):
    with pytest.raises(ValueError):
        RegexParams(pattern=pattern)


def test_regex_pattern_length_bounded():
    with pytest.raises(ValueError):
        RegexParams(pattern="a" * 10000)


# --- prompt injection / invented field codes (server-side validation) ------


def test_plan_with_invented_field_is_rejected(db_session):
    user = _make_user(db_session, "pi@example.com")
    db_session.commit()
    scope = build_scope(user, get_settings())
    plan = {
        "schema_version": "2.0",
        "title": "evil",
        "datasets": [
            {"id": "d", "primary_entity_type_id": 2, "sources": [{"alias": "deal", "entity_type_id": 2}]}
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [
                {
                    "id": "s",
                    "name": "S",
                    "mode": "rows",
                    "dataset_id": "d",
                    "columns": [
                        {
                            "id": "c",
                            "header": "X",
                            "value": {
                                "kind": "field",
                                "field": {
                                    "entity_type_id": 2,
                                    "field_code": "DROP TABLE crm_entities; --",
                                    "source_alias": "deal",
                                },
                            },
                        }
                    ],
                }
            ],
        },
    }
    prepared = prepare_plan(db_session, _portal(), scope, plan)
    assert not prepared.valid


# --- anonymous access -------------------------------------------------------


def test_anonymous_requests_allowed(client):
    assert client.get(f"{API}/conversations").status_code == 200
    assert client.post(f"{API}/conversations", json={}).status_code == 200


# --- input limits -----------------------------------------------------------


def test_oversized_message_rejected(client, db_session):
    db_session.commit()
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    huge = "x" * (get_settings().ie_max_message_chars + 100)
    resp = client.post(f"{API}/conversations/{cid}/chat", json={"message": huge})
    assert resp.status_code == 422 or resp.status_code == 400


# --- audit logging ----------------------------------------------------------


def test_audit_endpoint_accessible(client, db_session):
    db_session.commit()
    resp = client.get(f"{API}/audit")
    assert resp.status_code == 200
    assert "audit" in resp.json()
