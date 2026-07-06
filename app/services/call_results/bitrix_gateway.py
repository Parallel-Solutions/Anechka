"""Bitrix gateway for call results execution."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.exceptions import BitrixAPIError
from app.services.bitrix_client import BitrixClient
from app.utils.portal import absolute_bitrix_link, bitrix_task_url, parse_bitrix_external_id

logger = logging.getLogger(__name__)

_TASK_GET_SELECT = [
    "ID",
    "TITLE",
    "RESPONSIBLE_ID",
    "CREATED_BY",
    "createdBy",
    "UF_CRM_TASK",
    "crmItemIds",
    "DEADLINE",
    "STATUS",
    "link",
]


@dataclass
class GatewayResult:
    success: bool
    external_id: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    bitrix_task_url: str | None = None
    responsible_user_id: int | None = None


@dataclass(frozen=True)
class TaskVerifyResult:
    error: str | None = None
    responsible_user_id: int | None = None
    warning: str | None = None


class CallResultsBitrixGateway(Protocol):
    def add_deal_todo(self, payload: dict[str, Any]) -> GatewayResult: ...
    def add_task(self, payload: dict[str, Any]) -> GatewayResult: ...
    def get_task(self, task_id: int) -> GatewayResult: ...
    def create_verified_task(
        self,
        payload: dict[str, Any],
        *,
        deal_id: int,
        responsible_id: int,
        portal_id: str,
    ) -> GatewayResult: ...
    def add_deal_comment(self, payload: dict[str, Any]) -> GatewayResult: ...
    def find_contact_by_phone(self, phone: str) -> GatewayResult: ...
    def create_contact(self, fields: dict[str, Any]) -> GatewayResult: ...
    def update_contact_missing_fields(self, contact_id: int, fields: dict[str, Any]) -> GatewayResult: ...
    def ensure_contact_marker(self, contact_id: int, field_code: str, value: str) -> GatewayResult: ...
    def is_contact_linked_to_deal(self, deal_id: int, contact_id: int) -> bool: ...
    def link_contact_to_deal(self, deal_id: int, contact_id: int, *, is_primary: str = "N") -> GatewayResult: ...


def _norm_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return digits[-10:] if len(digits) >= 10 else digits


def _external_id_from_result(result: Any) -> str:
    if isinstance(result, dict):
        value = result.get("id") or result.get("ID")
        if value is not None:
            return str(value)
    if result is None:
        return ""
    return str(result)


def _parse_task_id_from_add_response(data: dict[str, Any]) -> int | None:
    if data.get("error") or data.get("error_description"):
        desc = data.get("error_description") or data.get("error")
        raise ValueError(str(desc))
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return None
    for container_key in ("task", "item"):
        container = result.get(container_key)
        if isinstance(container, dict):
            raw_id = container.get("id") or container.get("ID")
            if raw_id is not None and str(raw_id).isdigit():
                return int(raw_id)
    raw_id = result.get("id") or result.get("ID")
    if raw_id is not None and str(raw_id).isdigit():
        return int(raw_id)
    return None


def task_record_from_get_response(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return {}
    for key in ("task", "item"):
        nested = result.get(key)
        if isinstance(nested, dict):
            return nested
    if result.get("id") or result.get("ID"):
        return result
    return {}


def task_responsible_id_from_record(task_record: dict[str, Any]) -> int | None:
    raw = task_record.get("responsibleId") or task_record.get("RESPONSIBLE_ID")
    if raw is None:
        responsible = task_record.get("responsible")
        if isinstance(responsible, dict):
            raw = responsible.get("id") or responsible.get("ID")
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def task_created_by_id_from_record(task_record: dict[str, Any]) -> int | None:
    raw = task_record.get("createdBy") or task_record.get("CREATED_BY")
    if raw is None:
        creator = task_record.get("creator")
        if isinstance(creator, dict):
            raw = creator.get("id") or creator.get("ID")
    try:
        value = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _uf_crm_task_contains_deal(uf_crm_task: Any, deal_id: int) -> bool:
    expected = f"D_{deal_id}"
    if isinstance(uf_crm_task, str):
        return uf_crm_task == expected
    if isinstance(uf_crm_task, list):
        for item in uf_crm_task:
            if isinstance(item, str) and item == expected:
                return True
            if isinstance(item, dict):
                value = item.get("value") or item.get("VALUE") or item.get("id")
                if str(value) == expected:
                    return True
    return False


def task_crm_bindings_contain_deal(task_record: dict[str, Any], deal_id: int) -> bool:
    for key in ("ufCrmTask", "UF_CRM_TASK", "crmItemIds"):
        if _uf_crm_task_contains_deal(task_record.get(key), deal_id):
            return True
    return False


def resolve_task_url_from_record(
    portal_id: str,
    task_record: dict[str, Any],
    *,
    task_id: int,
    responsible_user_id: int | None = None,
) -> tuple[str | None, str | None]:
    api_link = absolute_bitrix_link(portal_id, task_record.get("link"))
    if api_link:
        return api_link, "api_link"

    verified = responsible_user_id or task_responsible_id_from_record(task_record)
    if verified and verified > 0:
        url = bitrix_task_url(portal_id, task_id, user_id=verified)
        if url:
            return url, "constructed"
    return None, None


def verify_created_task(
    task_record: dict[str, Any],
    *,
    task_id: int,
    deal_id: int,
    responsible_id: int,
) -> TaskVerifyResult:
    record_id = task_record.get("id") or task_record.get("ID")
    if record_id is None or int(record_id) != task_id:
        return TaskVerifyResult(
            error=f"Проверка задачи: ожидался ID {task_id}, получен {record_id}",
        )

    verified_resp = task_responsible_id_from_record(task_record)
    warning: str | None = None
    if verified_resp is None:
        return TaskVerifyResult(error="Проверка задачи: не удалось определить RESPONSIBLE_ID")
    if verified_resp != responsible_id:
        warning = (
            f"Bitrix назначил ответственного {verified_resp}, ожидался {responsible_id}"
        )

    verified_creator = task_created_by_id_from_record(task_record)
    if verified_creator is not None and verified_creator != responsible_id:
        creator_warning = (
            f"Bitrix назначил постановщика {verified_creator}, ожидался {responsible_id}"
        )
        warning = f"{warning}; {creator_warning}" if warning else creator_warning

    if not task_crm_bindings_contain_deal(task_record, deal_id):
        return TaskVerifyResult(
            error=f"Проверка задачи: привязка к сделке D_{deal_id} не найдена",
            responsible_user_id=verified_resp,
            warning=warning,
        )

    return TaskVerifyResult(responsible_user_id=verified_resp, warning=warning)


def enrich_task_response_payload(
    response_payload: dict[str, Any] | None,
    *,
    portal_id: str,
    task_record: dict[str, Any],
    task_id: int,
) -> dict[str, Any]:
    stored = dict(response_payload or {})
    if stored.get("bitrix_task_url") and stored.get("bitrix_task_link_source") == "api_link":
        return stored

    preferred_resp = stored.get("responsible_user_id")
    try:
        preferred_int = int(preferred_resp) if preferred_resp is not None else None
    except (TypeError, ValueError):
        preferred_int = None

    url, source = resolve_task_url_from_record(
        portal_id,
        task_record,
        task_id=task_id,
        responsible_user_id=preferred_int,
    )
    if not url:
        return stored

    verified_resp = task_responsible_id_from_record(task_record) or preferred_int
    stored["bitrix_task_url"] = url
    stored["bitrix_task_link_source"] = source
    if verified_resp:
        stored["responsible_user_id"] = verified_resp
    stored["bitrix_task_id"] = task_id
    return stored


class RealCallResultsBitrixGateway:
    def __init__(self, client: BitrixClient, *, marker_field: str = "", marker_value: str = ""):
        self.client = client
        self.marker_field = marker_field
        self.marker_value = marker_value

    def add_deal_todo(self, payload: dict[str, Any]) -> GatewayResult:
        try:
            data = self.client.call("crm.activity.todo.add", payload)
            return GatewayResult(
                success=True,
                external_id=_external_id_from_result(data.get("result")),
                response=data,
            )
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def add_task(self, payload: dict[str, Any]) -> GatewayResult:
        method = "tasks.task.add"
        try:
            data = self.client.call(method, payload)
            task_id = _parse_task_id_from_add_response(data)
            if not task_id:
                logger.error(
                    "Bitrix %s: no task id in response payload=%s response=%s",
                    method,
                    payload,
                    data,
                )
                return GatewayResult(
                    success=False,
                    error="Bitrix не вернул ID задачи",
                    response=data,
                )
            return GatewayResult(success=True, external_id=str(task_id), response=data)
        except BitrixAPIError as exc:
            logger.error(
                "Bitrix %s API error: %s (check webhook scope: tasks, crm, user)",
                method,
                exc,
            )
            return GatewayResult(success=False, error=str(exc))
        except Exception as exc:
            logger.error("Bitrix %s failed: %s", method, exc)
            return GatewayResult(success=False, error=str(exc))

    def get_task(self, task_id: int) -> GatewayResult:
        try:
            data = self.client.call(
                "tasks.task.get",
                {"taskId": task_id, "select": _TASK_GET_SELECT},
            )
            if data.get("error") or data.get("error_description"):
                desc = data.get("error_description") or data.get("error")
                return GatewayResult(success=False, error=str(desc), response=data)
            return GatewayResult(success=True, external_id=str(task_id), response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def _get_task_verified(self, task_id: int, deal_id: int) -> GatewayResult:
        get_res = self.get_task(task_id)
        if not get_res.success:
            return get_res
        task_record = task_record_from_get_response(get_res.response)
        if task_crm_bindings_contain_deal(task_record, deal_id):
            return get_res
        time.sleep(0.75)
        return self.get_task(task_id)

    def create_verified_task(
        self,
        payload: dict[str, Any],
        *,
        deal_id: int,
        responsible_id: int,
        portal_id: str,
    ) -> GatewayResult:
        add_res = self.add_task(payload)
        if not add_res.success:
            return add_res

        task_id = int(add_res.external_id or 0)
        if task_id <= 0:
            return GatewayResult(
                success=False,
                error="Bitrix не вернул ID задачи",
                response=add_res.response,
            )

        get_res = self._get_task_verified(task_id, deal_id)
        if not get_res.success:
            return GatewayResult(
                success=False,
                error=get_res.error or "Не удалось получить задачу после создания",
                external_id=str(task_id),
                response={"add_response": add_res.response, "get_response": get_res.response},
            )

        task_record = task_record_from_get_response(get_res.response)
        verify = verify_created_task(
            task_record,
            task_id=task_id,
            deal_id=deal_id,
            responsible_id=responsible_id,
        )
        if verify.error:
            return GatewayResult(
                success=False,
                error=verify.error,
                external_id=str(task_id),
                response={"add_response": add_res.response, "get_response": get_res.response},
            )

        url, link_source = resolve_task_url_from_record(
            portal_id,
            task_record,
            task_id=task_id,
            responsible_user_id=verify.responsible_user_id,
        )
        if not url:
            return GatewayResult(
                success=False,
                error="Не удалось сформировать ссылку на задачу",
                external_id=str(task_id),
                response={"add_response": add_res.response, "get_response": get_res.response},
            )

        fields = payload.get("fields") or {}
        response_body: dict[str, Any] = {
            "add_response": add_res.response,
            "get_response": get_res.response,
            "bitrix_task_id": task_id,
            "bitrix_task_url": url,
            "bitrix_task_link_source": link_source,
            "responsible_user_id": verify.responsible_user_id,
            "deal_id": deal_id,
            "task_title": fields.get("TITLE"),
            "task_description": fields.get("DESCRIPTION"),
            "deadline": fields.get("DEADLINE"),
            "status": "created",
        }
        if verify.warning:
            response_body["warning"] = verify.warning

        return GatewayResult(
            success=True,
            external_id=str(task_id),
            bitrix_task_url=url,
            responsible_user_id=verify.responsible_user_id,
            response=response_body,
        )

    def add_deal_comment(self, payload: dict[str, Any]) -> GatewayResult:
        try:
            data = self.client.call("crm.timeline.comment.add", payload)
            return GatewayResult(success=True, external_id=str(data.get("result", "")), response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def find_contact_by_phone(self, phone: str) -> GatewayResult:
        try:
            norm = _norm_phone(phone)
            data = self.client.call(
                "crm.contact.list",
                {
                    "filter": {"PHONE": phone},
                    "select": ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL"],
                },
            )
            items = data.get("result") or []
            for item in items:
                phones = item.get("PHONE") or []
                for p in phones if isinstance(phones, list) else []:
                    val = p.get("VALUE") if isinstance(p, dict) else str(p)
                    if _norm_phone(str(val)) == norm:
                        return GatewayResult(
                            success=True,
                            external_id=str(item.get("ID")),
                            response={"contact": item},
                        )
            return GatewayResult(success=True, external_id=None, response={"contact": None})
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def create_contact(self, fields: dict[str, Any]) -> GatewayResult:
        try:
            data = self.client.call("crm.contact.add", {"fields": fields})
            cid = str(data.get("result", ""))
            if self.marker_field and self.marker_value and cid:
                self.ensure_contact_marker(int(cid), self.marker_field, self.marker_value)
            return GatewayResult(success=True, external_id=cid, response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def update_contact_missing_fields(self, contact_id: int, fields: dict[str, Any]) -> GatewayResult:
        try:
            data = self.client.call("crm.contact.update", {"id": contact_id, "fields": fields})
            return GatewayResult(success=True, external_id=str(contact_id), response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def ensure_contact_marker(self, contact_id: int, field_code: str, value: str) -> GatewayResult:
        try:
            data = self.client.call(
                "crm.contact.update",
                {"id": contact_id, "fields": {field_code: value}},
            )
            return GatewayResult(success=True, external_id=str(contact_id), response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

    def is_contact_linked_to_deal(self, deal_id: int, contact_id: int) -> bool:
        try:
            data = self.client.call("crm.deal.contact.items.get", {"id": deal_id})
            items = data.get("result") or []
            return any(int(i.get("CONTACT_ID", 0)) == contact_id for i in items)
        except Exception:
            return False

    def link_contact_to_deal(self, deal_id: int, contact_id: int, *, is_primary: str = "N") -> GatewayResult:
        try:
            if self.is_contact_linked_to_deal(deal_id, contact_id):
                return GatewayResult(success=True, external_id=str(contact_id), response={"skipped": "already_linked"})
            data = self.client.call(
                "crm.deal.contact.add",
                {"id": deal_id, "fields": {"CONTACT_ID": contact_id, "IS_PRIMARY": is_primary}},
            )
            return GatewayResult(success=True, external_id=str(data.get("result", contact_id)), response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))


def maybe_refresh_task_action_url(
    action,
    *,
    portal_id: str,
    gateway: CallResultsBitrixGateway,
) -> bool:
    if action.method != "tasks.task.add" or action.execution_status != "succeeded":
        return False
    if not action.external_id:
        return False
    stored = action.response_payload or {}
    if stored.get("bitrix_task_url") and stored.get("bitrix_task_link_source") == "api_link":
        return False

    task_id = parse_bitrix_external_id(action.external_id)
    if not task_id:
        return False

    get_res = gateway.get_task(task_id)
    if not get_res.success:
        return False

    task_record = task_record_from_get_response(get_res.response)
    updated = enrich_task_response_payload(
        stored,
        portal_id=portal_id,
        task_record=task_record,
        task_id=task_id,
    )
    if updated == stored:
        return False
    action.response_payload = updated
    return True


def build_bitrix_gateway(settings: Settings, client: BitrixClient | None = None) -> CallResultsBitrixGateway:
    c = client or BitrixClient(settings)
    return RealCallResultsBitrixGateway(
        c,
        marker_field=getattr(settings, "bitrix_call_source_field_code", "") or "",
        marker_value=getattr(settings, "bitrix_call_source_field_value", "") or "",
    )
