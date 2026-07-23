"""Session cookie authentication middleware."""

from __future__ import annotations

from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

PUBLIC_PREFIXES = ("/static/", "/tomoru-hooks/")
PUBLIC_EXACT = {"/health", "/login", "/auth/login"}


def _is_public(path: str) -> bool:
    if path in PUBLIC_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _login_redirect(request: Request) -> RedirectResponse:
    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='')}", status_code=302)


def _unauthorized_api() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        content={"detail": {"code": "AUTH_REQUIRED", "message": "Требуется вход"}},
    )


def session_auth_required(request: Request, settings) -> Response | None:
    """Return a response when auth fails, otherwise None."""
    if settings.app_auth_disabled:
        return None
    path = request.url.path
    if _is_public(path):
        return None
    from app.database import SessionLocal
    from app.dependencies import get_app_settings
    from app.services.auth_service import AuthService

    db = SessionLocal()
    try:
        app_settings = get_app_settings(db)
        auth = AuthService(app_settings, db)
        token = request.cookies.get(app_settings.session_cookie_name)
        user = auth.load_session(token)
    finally:
        db.close()

    if user is not None:
        request.state.user = user
        return None

    if path.startswith("/api/") or path.startswith("/auth/"):
        return _unauthorized_api()
    return _login_redirect(request)
