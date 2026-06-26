"""Phase C: auth, session tokens, scoped repository ownership, plan versioning."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.repositories.intelligent_export_repository import (
    IeAccessDenied,
    IeNotFound,
    IntelligentExportRepository,
    PlanVersionConflict,
    ScopeContext,
)
from app.services.auth_service import AuthService, hash_password, verify_password


@pytest.fixture()
def auth(db_session):
    settings = get_settings()
    return AuthService(settings, db_session)


def _scope(user):
    return ScopeContext(user=user, portal_id=user.portal_id)


def test_password_hash_roundtrip():
    h = hash_password("s3cret!")
    assert h != "s3cret!"
    assert verify_password("s3cret!", h)
    assert not verify_password("wrong", h)


def test_create_authenticate_and_session(auth):
    user = auth.create_user("Analyst@Example.com", "pw12345", role="analyst", display_name="A")
    assert user.email == "analyst@example.com"
    assert auth.authenticate("analyst@example.com", "pw12345") is not None
    assert auth.authenticate("analyst@example.com", "bad") is None

    token = auth.issue_session(user)
    loaded = auth.load_session(token)
    assert loaded is not None and loaded.id == user.id
    assert auth.load_session("garbage") is None


def test_inactive_user_cannot_login_or_session(auth):
    user = auth.create_user("v@example.com", "pw12345", role="viewer")
    token = auth.issue_session(user)
    auth.set_active(user, False)
    assert auth.authenticate("v@example.com", "pw12345") is None
    assert auth.load_session(token) is None


def test_conversation_ownership_isolation(db_session, auth):
    alice = auth.create_user("alice@example.com", "pw12345", role="analyst")
    bob = auth.create_user("bob@example.com", "pw12345", role="analyst")
    repo_a = IntelligentExportRepository(db_session, _scope(alice))
    repo_b = IntelligentExportRepository(db_session, _scope(bob))

    conv = repo_a.create_conversation("Alice plan")
    # Bob cannot read Alice's conversation
    with pytest.raises(IeAccessDenied):
        repo_b.get_conversation(conv.id)
    # Alice can
    assert repo_a.get_conversation(conv.id).id == conv.id
    # Bob's listing does not include it
    assert conv.id not in {c.id for c in repo_b.list_conversations()}


def test_admin_can_access_any_conversation(db_session, auth):
    alice = auth.create_user("alice2@example.com", "pw12345", role="analyst")
    admin = auth.create_user("admin@example.com", "pw12345", role="admin")
    repo_a = IntelligentExportRepository(db_session, _scope(alice))
    repo_admin = IntelligentExportRepository(db_session, _scope(admin))
    conv = repo_a.create_conversation("Alice plan")
    assert repo_admin.get_conversation(conv.id).id == conv.id


def _minimal_plan(title="P"):
    return {
        "schema_version": "2.0",
        "title": title,
        "datasets": [{"id": "d", "primary_entity_type_id": 2, "sources": [{"alias": "deal", "entity_type_id": 2}]}],
        "workbook": {"format": "xlsx", "sheets": [{"id": "s", "name": "S", "mode": "rows", "dataset_id": "d", "columns": []}]},
    }


def test_plan_versions_are_immutable_and_incrementing(db_session, auth):
    user = auth.create_user("pv@example.com", "pw12345", role="analyst")
    repo = IntelligentExportRepository(db_session, _scope(user))
    conv = repo.create_conversation()
    v1 = repo.save_plan_version(conv.id, plan_json=_minimal_plan("v1"), validation_result_json={"status": "valid"}, catalog_snapshot_hash="h1")
    v2 = repo.save_plan_version(conv.id, plan_json=_minimal_plan("v2"), validation_result_json={"status": "valid"}, catalog_snapshot_hash="h1")
    assert v1.version_number == 1
    assert v2.version_number == 2
    db_session.refresh(conv)
    assert conv.current_plan_version_id == v2.id
    # old version untouched
    assert repo.get_plan_version(v1.id).plan_json["title"] == "v1"


def test_optimistic_concurrency_conflict(db_session, auth):
    user = auth.create_user("oc@example.com", "pw12345", role="analyst")
    repo = IntelligentExportRepository(db_session, _scope(user))
    conv = repo.create_conversation()
    repo.save_plan_version(conv.id, plan_json=_minimal_plan("v1"), validation_result_json=None, catalog_snapshot_hash="h")
    # client thinks current version is still 0 -> conflict (actual is 1)
    with pytest.raises(PlanVersionConflict):
        repo.save_plan_version(
            conv.id,
            plan_json=_minimal_plan("v2"),
            validation_result_json=None,
            catalog_snapshot_hash="h",
            expected_current_version_number=0,
        )
    # correct expectation succeeds
    v = repo.save_plan_version(
        conv.id,
        plan_json=_minimal_plan("v2"),
        validation_result_json=None,
        catalog_snapshot_hash="h",
        expected_current_version_number=1,
    )
    assert v.version_number == 2


def test_activate_old_version(db_session, auth):
    user = auth.create_user("av@example.com", "pw12345", role="analyst")
    repo = IntelligentExportRepository(db_session, _scope(user))
    conv = repo.create_conversation()
    v1 = repo.save_plan_version(conv.id, plan_json=_minimal_plan("v1"), validation_result_json=None, catalog_snapshot_hash="h")
    repo.save_plan_version(conv.id, plan_json=_minimal_plan("v2"), validation_result_json=None, catalog_snapshot_hash="h")
    repo.activate_plan_version(v1.id)
    db_session.refresh(conv)
    assert conv.current_plan_version_id == v1.id


def test_memory_visibility_user_vs_project(db_session, auth):
    alice = auth.create_user("ma@example.com", "pw12345", role="analyst")
    bob = auth.create_user("mb@example.com", "pw12345", role="analyst")
    repo_a = IntelligentExportRepository(db_session, _scope(alice))
    repo_b = IntelligentExportRepository(db_session, _scope(bob))

    project_mem = repo_a.create_memory(scope="project", kind="term", key="регион", content="...", value_json=None, status="approved")
    user_mem = repo_a.create_memory(scope="user", kind="preference", key="fmt", content="...", value_json=None, status="approved")

    bob_visible = {m.id for m in repo_b.list_memory()}
    assert project_mem.id in bob_visible  # project memory is shared
    assert user_mem.id not in bob_visible  # alice's user memory is private

    with pytest.raises(IeAccessDenied):
        repo_b.get_memory(user_mem.id)
