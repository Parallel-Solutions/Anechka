"""Portal identification from Bitrix webhook URL."""

from __future__ import annotations

import ast
import re
from typing import Any
from urllib.parse import urlparse


def portal_id_from_webhook(webhook_url: str) -> str:
    if not webhook_url:
        return "default"
    parsed = urlparse(webhook_url.rstrip("/"))
    host = parsed.netloc or parsed.path.split("/")[0]
    return host or "default"


def _valid_portal(portal_id: str) -> bool:
    return bool(portal_id and portal_id != "default")


def absolute_bitrix_link(portal_id: str, link: str | None) -> str | None:
    if not link or not str(link).strip():
        return None
    raw = str(link).strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if not _valid_portal(portal_id):
        return None
    if raw.startswith("/"):
        return f"https://{portal_id}{raw}"
    return f"https://{portal_id}/{raw.lstrip('/')}"


def bitrix_deal_url(portal_id: str, deal_id: int) -> str | None:
    if not _valid_portal(portal_id):
        return None
    return f"https://{portal_id}/crm/deal/details/{deal_id}/"


def bitrix_contact_url(portal_id: str, contact_id: int) -> str | None:
    if not _valid_portal(portal_id):
        return None
    return f"https://{portal_id}/crm/contact/details/{contact_id}/"


def bitrix_company_url(portal_id: str, company_id: int) -> str | None:
    if not _valid_portal(portal_id):
        return None
    return f"https://{portal_id}/crm/company/details/{company_id}/"


def bitrix_task_url(portal_id: str, task_id: int, *, user_id: int = 0) -> str | None:
    if not _valid_portal(portal_id) or task_id <= 0 or user_id <= 0:
        return None
    return f"https://{portal_id}/company/personal/user/{user_id}/tasks/task/view/{task_id}/"


def bitrix_activity_url(portal_id: str, activity_id: int) -> str | None:
    if not _valid_portal(portal_id) or activity_id <= 0:
        return None
    return f"https://{portal_id}/crm/activity/?ID={activity_id}/"


def parse_bitrix_external_id(external_id: str | None) -> int | None:
    if not external_id:
        return None
    raw = external_id.strip()
    if raw.isdigit():
        return int(raw)
    match = re.search(r"['\"]?id['\"]?\s*[:=]\s*(\d+)", raw, re.IGNORECASE)
    if match:
        return int(match.group(1))
    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return None
    if isinstance(parsed, dict):
        value = parsed.get("id") or parsed.get("ID")
        if value is not None and str(value).isdigit():
            return int(value)
    if isinstance(parsed, int):
        return parsed
    return None


def _responsible_user_id_from_payload(method: str, request_payload: dict[str, Any] | None) -> int:
    payload = request_payload or {}
    if method == "tasks.task.add":
        fields = payload.get("fields") or payload
        value = fields.get("RESPONSIBLE_ID") or fields.get("CREATED_BY")
    elif method == "crm.activity.todo.add":
        value = payload.get("responsibleId")
    else:
        value = None
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def bitrix_action_external_url(
    portal_id: str,
    method: str,
    external_id: str | None,
    *,
    deal_id: int | None = None,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
) -> str | None:
    if method == "tasks.task.add":
        stored_url = (response_payload or {}).get("bitrix_task_url")
        if stored_url:
            return str(stored_url)

    entity_id = parse_bitrix_external_id(external_id)
    if entity_id is None:
        return None
    if method == "tasks.task.add":
        user_id = 0
        if response_payload:
            raw = response_payload.get("responsible_user_id")
            try:
                user_id = int(raw) if raw is not None else 0
            except (TypeError, ValueError):
                user_id = 0
        if user_id <= 0:
            user_id = _responsible_user_id_from_payload(method, request_payload)
        return bitrix_task_url(portal_id, entity_id, user_id=user_id)
    if method == "crm.activity.todo.add":
        return bitrix_activity_url(portal_id, entity_id)
    if method in ("crm.contact.add", "crm.contact.list"):
        return bitrix_contact_url(portal_id, entity_id)
    if method == "crm.timeline.comment.add" and deal_id:
        return bitrix_deal_url(portal_id, deal_id)
    return None
