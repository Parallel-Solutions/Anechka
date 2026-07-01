"""In-memory Bitrix gateway for tests."""

from __future__ import annotations

import re
from typing import Any

from app.services.call_results.bitrix_gateway import GatewayResult


class FakeBitrixGateway:
    def __init__(self, *, fail_on: set[str] | None = None):
        self.contacts: dict[int, dict[str, Any]] = {}
        self.deal_links: dict[int, set[int]] = {}
        self.todos: list[dict] = []
        self.comments: list[dict] = []
        self.call_log: list[tuple[str, dict]] = []
        self.fail_on = fail_on or set()
        self._next_id = 1000

    def _next_contact_id(self) -> int:
        self._next_id += 1
        return self._next_id

    @staticmethod
    def _norm(phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        return digits[-10:] if len(digits) >= 10 else digits

    def add_deal_todo(self, payload: dict[str, Any]) -> GatewayResult:
        self.call_log.append(("crm.activity.todo.add", payload))
        if "crm.activity.todo.add" in self.fail_on:
            return GatewayResult(success=False, error="injected failure")
        eid = str(len(self.todos) + 1)
        self.todos.append(payload)
        return GatewayResult(success=True, external_id=eid, response={"result": eid})

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
