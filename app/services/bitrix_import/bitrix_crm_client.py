"""Extended Bitrix24 CRM client for import operations."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.config import Settings
from app.services.bitrix_client import BitrixClient
from app.utils.datetime_utils import parse_bitrix_datetime

logger = logging.getLogger(__name__)

ENTITY_TYPES = {
    1: "lead",
    2: "deal",
    3: "contact",
    4: "company",
}


@dataclass
class AccessDiagnostics:
    available_methods: list[str] = field(default_factory=list)
    unavailable_methods: list[str] = field(default_factory=list)
    unavailable_entities: list[str] = field(default_factory=list)
    permission_skips: list[str] = field(default_factory=list)
    unknown_field_types: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available_methods": self.available_methods,
            "unavailable_methods": self.unavailable_methods,
            "unavailable_entities": self.unavailable_entities,
            "permission_skips": self.permission_skips,
            "unknown_field_types": self.unknown_field_types,
            "failed_files": self.failed_files,
        }


class BitrixCrmClient(BitrixClient):
    """Bitrix client with crm.item.* and related import methods."""

    def __init__(
        self,
        settings: Settings,
        cancel_check: Callable[[], bool] | None = None,
    ):
        super().__init__(settings, cancel_check)
        self.api_requests_count = 0
        self.diagnostics = AccessDiagnostics()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.api_requests_count += 1
        try:
            result = super().call(method, params)
            if method not in self.diagnostics.available_methods:
                self.diagnostics.available_methods.append(method)
            return result
        except Exception as exc:
            if method not in self.diagnostics.unavailable_methods:
                self.diagnostics.unavailable_methods.append(method)
            raise exc

    def safe_call(self, method: str, params: dict | None = None) -> dict | None:
        try:
            return self.call(method, params)
        except Exception as exc:
            msg = f"{method}: {exc}"
            logger.warning("Safe call failed: %s", msg)
            self.diagnostics.permission_skips.append(msg)
            return None

    def get_item_fields(self, entity_type_id: int) -> dict[str, Any]:
        data = self.call(
            "crm.item.fields",
            {"entityTypeId": entity_type_id, "useOriginalUfNames": "Y"},
        )
        return data.get("result", {}).get("fields", data.get("result", {})) or {}

    def list_items_keyset(
        self,
        entity_type_id: int,
        cursor_time: datetime | None = None,
        cursor_id: int | None = None,
        batch_size: int = 50,
        filter_extra: dict | None = None,
    ):
        """Yield pages of items sorted by updatedTime ASC, id ASC."""
        filt: dict[str, Any] = dict(filter_extra or {})
        if cursor_time is not None:
            filt[">=updatedTime"] = cursor_time.isoformat()
        params: dict[str, Any] = {
            "entityTypeId": entity_type_id,
            "select": ["*"],
            "useOriginalUfNames": "Y",
            "order": {"updatedTime": "ASC", "id": "ASC"},
            "filter": filt,
        }
        start = 0
        while True:
            params["start"] = start
            data = self.call("crm.item.list", params)
            result = data.get("result", {})
            items = result.get("items", []) if isinstance(result, dict) else result
            if not items:
                break
            if cursor_time and cursor_id:
                items = [
                    i
                    for i in items
                    if self._item_after_cursor(i, cursor_time, cursor_id)
                ]
            if items:
                yield items
            if len(items) < batch_size:
                nxt = data.get("next")
                if nxt is None:
                    break
                start = nxt
            else:
                start = data.get("next", start + batch_size)
                if start is None:
                    break

    def list_all_item_ids(self, entity_type_id: int, batch_size: int = 50) -> list[int]:
        ids: list[int] = []
        params: dict[str, Any] = {
            "entityTypeId": entity_type_id,
            "select": ["id"],
            "order": {"id": "ASC"},
        }
        start = 0
        while True:
            params["start"] = start
            data = self.call("crm.item.list", params)
            result = data.get("result", {})
            items = result.get("items", []) if isinstance(result, dict) else result
            if not items:
                break
            for item in items:
                iid = item.get("id") or item.get("ID")
                if iid:
                    ids.append(int(iid))
            nxt = data.get("next")
            if nxt is None:
                break
            start = nxt
        return ids

    def get_item(self, entity_type_id: int, entity_id: int) -> dict | None:
        data = self.safe_call(
            "crm.item.get",
            {"entityTypeId": entity_type_id, "id": entity_id, "useOriginalUfNames": "Y"},
        )
        if not data:
            return None
        result = data.get("result", {})
        return result.get("item", result) if isinstance(result, dict) else result

    def get_deal_contact_items(self, deal_id: int) -> list[dict]:
        data = self.safe_call("crm.deal.contact.items.get", {"id": deal_id})
        return (data or {}).get("result", []) or []

    def get_lead_contact_items(self, lead_id: int) -> list[dict]:
        data = self.safe_call("crm.lead.contact.items.get", {"id": lead_id})
        return (data or {}).get("result", []) or []

    def batch_lead_contacts(self, lead_ids: list[int]) -> dict[int, list[dict]]:
        results: dict[int, list[dict]] = {}
        if not lead_ids:
            return results
        commands = [("crm.lead.contact.items.get", {"id": lid}) for lid in lead_ids]
        batch_results = self.batch(commands)
        for idx, lid in enumerate(lead_ids):
            items = batch_results.get(f"cmd{idx}") or []
            results[lid] = items if isinstance(items, list) else ([items] if items else [])
        return results

    @staticmethod
    def _item_after_cursor(item: dict, cursor_time: datetime, cursor_id: int) -> bool:
        ut = parse_bitrix_datetime(str(item.get("updatedTime") or ""))
        iid = int(item.get("id") or item.get("ID") or 0)
        if ut is None:
            return iid > cursor_id
        if ut > cursor_time:
            return True
        if ut == cursor_time:
            return iid > cursor_id
        return False

    def list_statuses(self, entity_id: str | None = None) -> list[dict]:
        filt = {"ENTITY_ID": entity_id} if entity_id else {}
        return self.get_paginated("crm.status.list", {"filter": filt, "order": {"SORT": "ASC"}})

    def list_all_categories(self, entity_type_id: int = 2) -> list[dict]:
        data = self.safe_call("crm.category.list", {"entityTypeId": entity_type_id})
        if not data:
            return []
        return data.get("result", {}).get("categories", [])

    def list_currencies(self) -> list[dict]:
        data = self.safe_call("crm.currency.list", {})
        if not data:
            return []
        result = data.get("result", [])
        return result if isinstance(result, list) else list(result.values()) if isinstance(result, dict) else []

    def list_product_rows(self, entity_type_id: int, entity_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.item.productrow.list",
            {"entityTypeId": entity_type_id, "id": entity_id},
        )
        if not data:
            return []
        result = data.get("result", {})
        rows = result.get("productRows", result) if isinstance(result, dict) else result
        return rows if isinstance(rows, list) else []

    def list_activities(self, owner_type_id: int, owner_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.activity.list",
            {
                "filter": {"OWNER_TYPE_ID": owner_type_id, "OWNER_ID": owner_id},
                "select": ["*"],
            },
        )
        if not data:
            return []
        return data.get("result", []) or []

    def list_timeline_comments(self, entity_type_id: int, entity_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.timeline.comment.list",
            {"filter": {"ENTITY_TYPE_ID": entity_type_id, "ENTITY_ID": entity_id}},
        )
        if not data:
            return []
        return data.get("result", []) or []

    def list_stage_history(self, entity_type_id: int, entity_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.stagehistory.list",
            {"entityTypeId": entity_type_id, "filter": {"OWNER_ID": entity_id}},
        )
        if not data:
            return []
        result = data.get("result", {})
        items = result.get("items", result) if isinstance(result, dict) else result
        return items if isinstance(items, list) else []

    def list_requisites(self, entity_type_id: int, entity_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.requisite.list",
            {"filter": {"ENTITY_TYPE_ID": entity_type_id, "ENTITY_ID": entity_id}},
        )
        if not data:
            return []
        return data.get("result", []) or []

    def list_addresses(self, entity_type_id: int, entity_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.address.list",
            {"filter": {"ENTITY_TYPE_ID": entity_type_id, "ENTITY_ID": entity_id}},
        )
        if not data:
            return []
        return data.get("result", []) or []

    def list_bank_details(self, requisite_id: int) -> list[dict]:
        data = self.safe_call(
            "crm.requisite.bankdetail.list",
            {"filter": {"ENTITY_ID": requisite_id}},
        )
        if not data:
            return []
        return data.get("result", []) or []

    def download_file_content(self, file_id: str | int) -> tuple[bytes | None, dict]:
        data = self.safe_call("disk.file.get", {"id": file_id})
        metadata: dict[str, Any] = {}
        if not data:
            self.diagnostics.failed_files.append(str(file_id))
            return None, metadata
        result = data.get("result", {})
        metadata = result if isinstance(result, dict) else {}
        download_url = metadata.get("DOWNLOAD_URL") or metadata.get("downloadUrl")
        if not download_url:
            self.diagnostics.failed_files.append(str(file_id))
            return None, metadata
        try:
            import requests

            resp = self.session.get(
                download_url,
                timeout=(self.settings.connect_timeout, self.settings.read_timeout),
            )
            resp.raise_for_status()
            return resp.content, metadata
        except Exception as exc:
            logger.warning("File download failed for %s: %s", file_id, exc)
            self.diagnostics.failed_files.append(str(file_id))
            return None, metadata
