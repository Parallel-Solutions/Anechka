"""In-memory Bitrix gateway for tests."""

from __future__ import annotations

import re
from typing import Any

from app.services.call_results.bitrix_gateway import (
    GatewayResult,
    resolve_task_url_from_record,
    task_record_from_get_response,
    verify_created_task,
)
from app.utils.portal import absolute_bitrix_link


class FakeBitrixGateway:
    def __init__(self, *, fail_on: set[str] | None = None, empty_task_id: bool = False, verify_mismatch: bool = False):
        self.contacts: dict[int, dict[str, Any]] = {}
        self.deal_links: dict[int, set[int]] = {}
        self.todos: list[dict] = []
        self.tasks: list[dict] = []
        self.task_records: dict[int, dict[str, Any]] = {}
        self.comments: list[dict] = []
        self.call_log: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()
        self.empty_task_id = empty_task_id
        self.verify_mismatch = verify_mismatch
        self._next_id = 1000

    def _next_contact_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def _norm(phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        return digits[-10:] if len(digits) >= 10 else digits

    def _task_link(self, portal_id: str, task_id: int, responsible_id: int) -> str:
        return absolute_bitrix_link(
            portal_id,
            f"/company/personal/user/{responsible_id}/tasks/task/view/{task_id}/",
        ) or ""

    def add_deal_todo(self, payload: dict[str, Any]) -> GatewayResult:
        self.call_log.append(("crm.activity.todo.add", payload))
        if "crm.activity.todo.add" in self.fail_on:
            return GatewayResult(success=False, error="injected failure")
        eid = str(len(self.todos) + 1)
        self.todos.append(payload)
        return GatewayResult(success=True, external_id=eid, response={"result": eid})

    def add_task(self, payload: dict[str, Any]) -> GatewayResult:
        self.call_log.append(("tasks.task.add", payload))
        if "tasks.task.add" in self.fail_on:
            return GatewayResult(success=False, error="injected failure")
        if self.empty_task_id:
            return GatewayResult(success=False, error="Bitrix не вернул ID задачи", response={"result": {}})
        eid = len(self.tasks) + 1
        self.tasks.append(payload)
        fields = payload.get("fields") or payload
        responsible_id = int(fields.get("RESPONSIBLE_ID") or 0)
        uf_crm = fields.get("UF_CRM_TASK") or []
        if self.verify_mismatch:
            responsible_id = responsible_id + 1
        record = {
            "id": eid,
            "ID": eid,
            "TITLE": fields.get("TITLE"),
            "responsibleId": responsible_id,
            "RESPONSIBLE_ID": responsible_id,
            "ufCrmTask": uf_crm,
            "UF_CRM_TASK": uf_crm,
            "DEADLINE": fields.get("DEADLINE"),
            "STATUS": "2",
            "link": f"/company/personal/user/{responsible_id}/tasks/task/view/{eid}/",
        }
        self.task_records[eid] = record
        return GatewayResult(success=True, external_id=str(eid), response={"result": {"task": {"id": eid}}})

    def get_task(self, task_id: int) -> GatewayResult:
        self.call_log.append(("tasks.task.get", {"taskId": task_id}))
        if "tasks.task.get" in self.fail_on:
            return GatewayResult(success=False, error="injected get failure")
        record = self.task_records.get(task_id)
        if not record:
            return GatewayResult(success=False, error=f"Task {task_id} not found")
        return GatewayResult(
            success=True,
            external_id=str(task_id),
            response={"result": {"task": dict(record)}},
        )

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
        get_res = self.get_task(task_id)
        if not get_res.success:
            return GatewayResult(
                success=False,
                error=get_res.error,
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
            )
        fields = payload.get("fields") or {}
        response_body: dict[str, Any] = {
            "add_response": add_res.response,
            "get_response": get_res.response,
            "call_id": None,
            "deal_id": deal_id,
            "responsible_user_id": verify.responsible_user_id,
            "bitrix_task_id": task_id,
            "bitrix_task_url": url,
            "bitrix_task_link_source": link_source,
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
        self.call_log.append(("crm.timeline.comment.add", payload))
        if "crm.timeline.comment.add" in self.fail_on:
            return GatewayResult(success=False, error="injected failure")
        eid = str(len(self.comments) + 1)
        self.comments.append(payload)
        return GatewayResult(success=True, external_id=eid, response={"result": eid})

    def find_contact_by_phone(self, phone: str) -> GatewayResult:
        self.call_log.append(("crm.contact.list", {"phone": phone}))
        norm = self._norm(phone)
        for cid, c in self.contacts.items():
            for p in c.get("PHONE", []):
                val = p.get("VALUE") if isinstance(p, dict) else str(p)
                if self._norm(str(val)) == norm:
                    return GatewayResult(success=True, external_id=str(cid), response={"contact": c})
        return GatewayResult(success=True, external_id=None, response={"contact": None})

    def create_contact(self, fields: dict[str, Any]) -> GatewayResult:
        self.call_log.append(("crm.contact.add", fields))
        cid = self._next_contact_id()
        self.contacts[cid] = {"ID": cid, **fields}
        return GatewayResult(success=True, external_id=str(cid), response={"result": cid})

    def update_contact_missing_fields(self, contact_id: int, fields: dict[str, Any]) -> GatewayResult:
        self.call_log.append(("crm.contact.update", {"id": contact_id, "fields": fields}))
        existing = self.contacts.get(contact_id, {"ID": contact_id})
        for k, v in fields.items():
            if v and not existing.get(k):
                existing[k] = v
        self.contacts[contact_id] = existing
        return GatewayResult(success=True, external_id=str(contact_id))

    def ensure_contact_marker(self, contact_id: int, field_code: str, value: str) -> GatewayResult:
        self.call_log.append(("marker", {"id": contact_id, field_code: value}))
        c = self.contacts.setdefault(contact_id, {"ID": contact_id})
        c[field_code] = value
        return GatewayResult(success=True, external_id=str(contact_id))

    def is_contact_linked_to_deal(self, deal_id: int, contact_id: int) -> bool:
        return contact_id in self.deal_links.get(deal_id, set())

    def link_contact_to_deal(self, deal_id: int, contact_id: int, *, is_primary: str = "N") -> GatewayResult:
        self.call_log.append(("crm.deal.contact.add", {"deal_id": deal_id, "contact_id": contact_id}))
        self.deal_links.setdefault(deal_id, set()).add(contact_id)
        return GatewayResult(success=True, external_id=str(contact_id))
