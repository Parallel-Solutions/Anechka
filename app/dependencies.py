"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings, merge_db_settings
from app.database import get_db
from app.models import AppUser
from app.services.auth_service import AuthService
from app.services.settings_service import load_settings_from_db

ROLE_RANK = {"viewer": 1, "analyst": 2, "admin": 3}


def get_session() -> Generator[Session, None, None]:
    yield from get_db()


def get_app_settings(db: Session) -> Settings:
    db_values = load_settings_from_db(db)
    return merge_db_settings(db_values)


def get_settings_dep(db: Session = Depends(get_session)) -> Settings:
    return get_app_settings(db)


def get_auth_service(db: Session = Depends(get_session)) -> AuthService:
    return AuthService(get_app_settings(db), db)


def get_optional_user(request: Request, auth: AuthService = Depends(get_auth_service)) -> AppUser | None:
    token = request.cookies.get(auth.settings.session_cookie_name)
    return auth.load_session(token)


def get_current_user(user: AppUser | None = Depends(get_optional_user)) -> AppUser:
    if user is None:
        raise HTTPException(status_code=401, detail={"code": "AUTH_REQUIRED", "message": "Требуется вход"})
    return user


def require_role(minimum: str):
    min_rank = ROLE_RANK.get(minimum, 99)

    def _dep(user: AppUser = Depends(get_current_user)) -> AppUser:
        if ROLE_RANK.get(user.role, 0) < min_rank:
            raise HTTPException(
                status_code=403,
                detail={"code": "ACCESS_DENIED", "message": "Недостаточно прав"},
            )
        return user

    return _dep


def get_ie_user(auth: AuthService = Depends(get_auth_service)) -> AppUser:
    return auth.get_default_ie_user()


def get_call_result_classifier_instance(settings: Settings):
    from app.services.call_results.fake_classifier import FakeCallResultClassifier
    from app.services.call_results.llm_gateway import DisabledCallResultClassifier, OpenAICallResultClassifier

    if settings.llm_call_results_use_mock:
        return FakeCallResultClassifier()
    if not settings.llm_call_results_enabled:
        return DisabledCallResultClassifier()
    return OpenAICallResultClassifier(settings)


def get_call_result_classifier(settings: Settings = Depends(get_settings_dep)):
    return get_call_result_classifier_instance(settings)
