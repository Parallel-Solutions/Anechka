"""Auth routes and app-user management API."""

from __future__ import annotations

from app.services.auth_service import AuthService


def test_create_and_list_users(auth_client, db_session):
    from app.config import get_settings

    auth = AuthService(get_settings(), db_session)
    auth.create_user("second@example.com", "secret12", display_name="Second")

    resp = auth_client.get("/api/app-users")
    assert resp.status_code == 200
    emails = {u["email"] for u in resp.json()["users"]}
    assert "test@example.com" in emails
    assert "second@example.com" in emails


def test_create_user_validation(auth_client):
    resp = auth_client.post(
        "/api/app-users",
        json={"email": "bad@example.com", "password": "123"},
    )
    assert resp.status_code == 422


def test_create_user_with_bitrix_id(auth_client):
    resp = auth_client.post(
        "/api/app-users",
        json={
            "email": "bitrix@example.com",
            "password": "secret12",
            "display_name": "Bitrix User",
            "crm_user_external_id": 42,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["user"]["crm_user_external_id"] == 42

    listed = auth_client.get("/api/app-users").json()["users"]
    match = next(u for u in listed if u["email"] == "bitrix@example.com")
    assert match["crm_user_external_id"] == 42


def test_cannot_deactivate_self(auth_client, db_session):
    from app.config import get_settings

    me = auth_client.get("/auth/me").json()["user"]
    resp = auth_client.patch(f"/api/app-users/{me['id']}", json={"is_active": False})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "SELF_DEACTIVATE"


def test_reset_password(auth_client, db_session):
    from app.config import get_settings

    auth = AuthService(get_settings(), db_session)
    other = auth.create_user("reset@example.com", "oldpass1")

    resp = auth_client.patch(f"/api/app-users/{other.id}", json={"password": "newpass1"})
    assert resp.status_code == 200
    db_session.expire_all()
    assert AuthService(get_settings(), db_session).authenticate("reset@example.com", "newpass1") is not None
