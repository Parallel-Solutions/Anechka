"""Stable error codes for the intelligent export API."""

from __future__ import annotations

from fastapi import HTTPException

ERROR_STATUS = {
    "AUTH_REQUIRED": 401,
    "ACCESS_DENIED": 403,
    "CONVERSATION_NOT_FOUND": 404,
    "PLAN_NOT_FOUND": 404,
    "PROMPT_NOT_FOUND": 404,
    "MEMORY_NOT_FOUND": 404,
    "RUN_NOT_FOUND": 404,
    "PLAN_VERSION_CONFLICT": 409,
    "PLAN_INVALID": 422,
    "FIELD_NOT_ALLOWED": 422,
    "RELATION_NOT_ALLOWED": 422,
    "IMPORT_STALE": 409,
    "NO_DATA": 409,
    "CATALOG_EMPTY": 409,
    "ROW_LIMIT_EXCEEDED": 422,
    "EXPORT_NOT_READY": 409,
    "AI_UNAVAILABLE": 503,
    "VALIDATION_ERROR": 400,
    "PREVIEW_FAILED": 422,
    "QUERY_TIMEOUT": 504,
}


def ie_error(code: str, message: str, *, extra: dict | None = None) -> HTTPException:
    detail = {"code": code, "message": message}
    if extra:
        detail.update(extra)
    return HTTPException(status_code=ERROR_STATUS.get(code, 400), detail=detail)
