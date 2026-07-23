"""Authentication and app-user management."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.dependencies import get_auth_service, get_current_user, get_session, require_role
from app.models import AppUser
from app.services.auth_service import AuthService

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: str
    is_active: bool
    crm_user_external_id: int | None = None

    @classmethod
    def from_user(cls, user: AppUser) -> UserOut:
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            crm_user_external_id=user.crm_user_external_id,
        )


class CreateUserRequest(BaseModel):
    email: str
    password: str = Field(min_length=6)
    display_name: str = ""
    role: Literal["admin", "analyst", "viewer"] = "viewer"
    crm_user_external_id: int | None = None


class UpdateUserRequest(BaseModel):
    display_name: str | None = None
    password: str | None = Field(default=None, min_length=6)
    role: Literal["admin", "analyst", "viewer"] | None = None
    is_active: bool | None = None


def _set_session_cookie(response: Response, auth: AuthService, user: AppUser) -> None:
    token = auth.issue_session(user)
    response.set_cookie(
        key=auth.settings.session_cookie_name,
        value=token,
        max_age=auth.settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=auth.settings.cookie_secure,
        path="/",
    )


def _clear_session_cookie(response: Response, auth: AuthService) -> None:
    response.delete_cookie(
        key=auth.settings.session_cookie_name,
        path="/",
        secure=auth.settings.cookie_secure,
    )


@router.post("/auth/login")
def login(
    body: LoginRequest,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> dict[str, Any]:
    user = auth.authenticate(body.email, body.password)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "INVALID_CREDENTIALS", "message": "Неверный email или пароль"},
        )
    _set_session_cookie(response, auth, user)
    return {"user": UserOut.from_user(user).model_dump()}


@router.post("/auth/logout")
def logout(response: Response, auth: AuthService = Depends(get_auth_service)) -> dict[str, str]:
    _clear_session_cookie(response, auth)
    return {"status": "ok"}


@router.get("/auth/me")
def me(user: AppUser = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": UserOut.from_user(user).model_dump()}


@router.get("/api/app-users")
def list_app_users(
    auth: AuthService = Depends(get_auth_service),
    _admin: AppUser = Depends(require_role("admin")),
) -> dict[str, list[dict[str, Any]]]:
    users = auth.list_users()
    return {"users": [UserOut.from_user(u).model_dump() for u in users]}


@router.post("/api/app-users", status_code=201)
def create_app_user(
    body: CreateUserRequest,
    auth: AuthService = Depends(get_auth_service),
    _admin: AppUser = Depends(require_role("admin")),
) -> dict[str, Any]:
    try:
        created = auth.create_user(
            body.email,
            body.password,
            role=body.role,
            display_name=body.display_name,
            crm_user_external_id=body.crm_user_external_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}) from exc
    return {"user": UserOut.from_user(created).model_dump()}


@router.patch("/api/app-users/{user_id}")
def update_app_user(
    user_id: int,
    body: UpdateUserRequest,
    auth: AuthService = Depends(get_auth_service),
    current: AppUser = Depends(require_role("admin")),
) -> dict[str, Any]:
    target = auth.get_user(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Пользователь не найден"})
    if body.is_active is False and target.id == current.id:
        raise HTTPException(
            status_code=400,
            detail={"code": "SELF_DEACTIVATE", "message": "Нельзя деактивировать себя"},
        )
    if body.role is not None and body.role != "admin" and target.id == current.id:
        raise HTTPException(
            status_code=400,
            detail={"code": "SELF_DEMOTE", "message": "Нельзя снять роль администратора у себя"},
        )
    try:
        updated = auth.update_user(
            target,
            display_name=body.display_name,
            password=body.password,
            role=body.role,
            is_active=body.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "VALIDATION_ERROR", "message": str(exc)}) from exc
    return {"user": UserOut.from_user(updated).model_dump()}
