"""Bitrix gateway for call results execution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.config import Settings
from app.services.bitrix_client import BitrixClient


@dataclass
class GatewayResult:
    success: bool
    external_id: str | None = None
    response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class CallResultsBitrixGateway(Protocol):
    def add_deal_todo(self, payload: dict[str, Any]) -> GatewayResult: ...
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


class RealCallResultsBitrixGateway:
    def __init__(self, client: BitrixClient, *, marker_field: str = "", marker_value: str = ""):
        self.client = client
        self.marker_field = marker_field
        self.marker_value = marker_value

    def add_deal_todo(self, payload: dict[str, Any]) -> GatewayResult:
        try:
            data = self.client.call("crm.activity.todo.add", payload)
            return GatewayResult(success=True, external_id=str(data.get("result", "")), response=data)
        except Exception as exc:
            return GatewayResult(success=False, error=str(exc))

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


def build_bitrix_gateway(settings: Settings, client: BitrixClient | None = None) -> CallResultsBitrixGateway:
    c = client or BitrixClient(settings)
    return RealCallResultsBitrixGateway(
        c,
        marker_field=getattr(settings, "bitrix_call_source_field_code", "") or "",
        marker_value=getattr(settings, "bitrix_call_source_field_value", "") or "",
    )
