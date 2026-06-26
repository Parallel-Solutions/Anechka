"""Authentication: app users, password hashing, signed session cookies.

Sessions are stateless signed tokens (itsdangerous) bound to ``APP_SECRET_KEY``.
Passwords are hashed with bcrypt. This is an app-level identity model that is
intentionally separate from Bitrix users (see ADR-001): local roles do NOT
reproduce Bitrix24 ACL.
"""

from __future__ import annotations

import logging

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppUser, utcnow
from app.models.intelligent_export import APP_ROLES
from app.utils.portal import portal_id_from_webhook

logger = logging.getLogger(__name__)

SESSION_SALT = "ie-session-v1"
DEFAULT_IE_USER_EMAIL = "system@local"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


def resolve_portal_id(settings: Settings) -> str:
    if settings.bitrix_webhook_url:
        return portal_id_from_webhook(settings.bitrix_webhook_url)
    return settings.ie_default_portal_id or "default"


class AuthService:
    def __init__(self, settings: Settings, db: Session):
        self.settings = settings
        self.db = db
        self.portal_id = resolve_portal_id(settings)

    # --- session tokens -----------------------------------------------------
    def _serializer(self) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(self.settings.app_secret_key, salt=SESSION_SALT)

    def issue_session(self, user: AppUser) -> str:
        return self._serializer().dumps({"uid": user.id, "portal": user.portal_id})

    def load_session(self, token: str | None) -> AppUser | None:
        if not token:
            return None
        try:
            data = self._serializer().loads(token, max_age=self.settings.session_max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(data, dict) or data.get("portal") != self.portal_id:
            return None
        user = self.db.get(AppUser, int(data.get("uid", 0)))
        if user is None or not user.is_active or user.portal_id != self.portal_id:
            return None
        return user

    # --- users --------------------------------------------------------------
    def authenticate(self, email: str, password: str) -> AppUser | None:
        user = self.db.scalar(
            select(AppUser).where(AppUser.portal_id == self.portal_id, AppUser.email == email.strip().lower())
        )
        if user is None or not user.is_active:
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    def get_user(self, user_id: int) -> AppUser | None:
        user = self.db.get(AppUser, user_id)
        if user and user.portal_id == self.portal_id:
            return user
        return None

    def list_users(self) -> list[AppUser]:
        return list(self.db.scalars(select(AppUser).where(AppUser.portal_id == self.portal_id).order_by(AppUser.id)))

    def create_user(
        self,
        email: str,
        password: str,
        role: str = "viewer",
        display_name: str = "",
        crm_user_external_id: int | None = None,
    ) -> AppUser:
        if role not in APP_ROLES:
            raise ValueError(f"Unknown role: {role}")
        email = email.strip().lower()
        existing = self.db.scalar(
            select(AppUser).where(AppUser.portal_id == self.portal_id, AppUser.email == email)
        )
        if existing is not None:
            raise ValueError("Пользователь с таким email уже существует")
        user = AppUser(
            portal_id=self.portal_id,
            email=email,
            password_hash=hash_password(password),
            display_name=display_name or email,
            role=role,
            crm_user_external_id=crm_user_external_id,
            is_active=True,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def set_active(self, user: AppUser, active: bool) -> AppUser:
        user.is_active = active
        user.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(user)
        return user

    def set_role(self, user: AppUser, role: str) -> AppUser:
        if role not in APP_ROLES:
            raise ValueError(f"Unknown role: {role}")
        user.role = role
        user.updated_at = utcnow()
        self.db.commit()
        self.db.refresh(user)
        return user

    def ensure_bootstrap_admin(self) -> None:
        email = (self.settings.bootstrap_admin_email or "").strip().lower()
        password = self.settings.bootstrap_admin_password
        if not email or not password:
            return
        existing = self.db.scalar(
            select(AppUser).where(AppUser.portal_id == self.portal_id, AppUser.email == email)
        )
        if existing is not None:
            return
        try:
            self.create_user(email, password, role="admin", display_name="Administrator")
            logger.info("Bootstrap admin created for portal %s", self.portal_id)
        except ValueError:
            self.db.rollback()

    def ensure_default_ie_user(self) -> AppUser:
        email = DEFAULT_IE_USER_EMAIL
        existing = self.db.scalar(
            select(AppUser).where(AppUser.portal_id == self.portal_id, AppUser.email == email)
        )
        if existing is not None:
            return existing
        user = AppUser(
            portal_id=self.portal_id,
            email=email,
            password_hash=hash_password("unused"),
            display_name="System",
            role="admin",
            is_active=True,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        logger.info("Default IE user created for portal %s", self.portal_id)
        return user

    def get_default_ie_user(self) -> AppUser:
        return self.ensure_default_ie_user()
