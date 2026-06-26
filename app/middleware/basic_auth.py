"""HTTP Basic Auth helpers."""

from __future__ import annotations

import base64
import binascii
import secrets

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

REALM = "Bitrix24 Export"


def unauthorized_response() -> Response:
    return JSONResponse(
        status_code=401,
        content={"detail": "Unauthorized"},
        headers={"WWW-Authenticate": f'Basic realm="{REALM}"'},
    )


def check_basic_credentials(auth_header: str | None, username: str, password: str) -> bool:
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    user, sep, pwd = decoded.partition(":")
    if not sep:
        return False
    return secrets.compare_digest(user, username) and secrets.compare_digest(pwd, password)


def basic_auth_required(request: Request, username: str, password: str) -> Response | None:
    """Return 401 response when auth fails, otherwise None."""
    if request.url.path == "/health":
        return None
    auth_header = request.headers.get("authorization")
    if check_basic_credentials(auth_header, username, password):
        return None
    return unauthorized_response()
