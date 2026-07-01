"""Клиент Bitrix24 REST API."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from app.config import Settings
from app.exceptions import (
    BitrixAPIError,
    BitrixAuthenticationError,
    BitrixRateLimitError,
    ExportCancelledError,
)
from app.services.security_service import mask_webhook

logger = logging.getLogger(__name__)

RATE_LIMIT_ERRORS = {"QUERY_LIMIT_EXCEEDED", "OVERLOAD_LIMIT"}
AUTH_ERRORS = {"INVALID_CREDENTIALS", "AUTHORIZATION_ERROR", "ACCESS_DENIED"}
RETRY_HTTP_CODES = {429, 500, 502, 503, 504}
BATCH_SIZE = 50
PAGE_SIZE = 50

ENTITY_FIELD_METHODS = {
    "deal": "crm.deal.fields",
    "contact": "crm.contact.fields",
    "company": "crm.company.fields",
}


class BitrixClient:
    def __init__(self, settings: Settings, cancel_check: Callable[[], bool] | None = None):
        self.settings = settings
        self.cancel_check = cancel_check or (lambda: False)
        self.session = requests.Session()
        self.base_url = settings.bitrix_webhook_url.rstrip("/")
        self.users_cache: dict[int, dict[str, Any]] = {}
        self.contacts_cache: dict[int, dict[str, Any]] = {}
        self.companies_cache: dict[int, dict[str, Any]] = {}
        self.company_contacts_cache: dict[int, list[int]] = {}
        self.deal_contacts_cache: dict[int, list[int]] = {}

    def _should_retry(self, response: requests.Response | None, data: dict | None) -> bool:
        if response is not None and response.status_code in RETRY_HTTP_CODES:
            return True
        if data:
            err = str(data.get("error", ""))
            if err in RATE_LIMIT_ERRORS:
                return True
        return False

    def _raise_bitrix_error(self, data: dict[str, Any]) -> None:
        err = str(data.get("error", ""))
        desc = data.get("error_description", err)
        if err in AUTH_ERRORS:
            raise BitrixAuthenticationError(desc)
        if err in RATE_LIMIT_ERRORS:
            raise BitrixRateLimitError(desc)
        raise BitrixAPIError(desc)

    def call(self, method: str, params: dict | None = None) -> dict:
        if not self.base_url:
            raise BitrixAuthenticationError("URL вебхука не настроен")
        url = f"{self.base_url}/{method}"
        payload = params or {}
        last_exc: Exception | None = None

        for attempt in range(self.settings.max_retries + 1):
            if self.cancel_check():
                raise ExportCancelledError()
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    timeout=(self.settings.connect_timeout, self.settings.read_timeout),
                )
                data: dict[str, Any] = {}
                try:
                    data = response.json()
                except ValueError:
                    response.raise_for_status()
                    raise BitrixAPIError(f"Некорректный JSON от Bitrix24 для {method}")

                if "error" in data:
                    err = str(data.get("error", ""))
                    if err in RATE_LIMIT_ERRORS and attempt < self.settings.max_retries:
                        delay = self.settings.retry_base_delay * (2**attempt)
                        logger.warning(
                            "Retry %s attempt %s, delay %.1fs, webhook=%s",
                            method,
                            attempt + 1,
                            delay,
                            mask_webhook(self.base_url),
                        )
                        time.sleep(delay)
                        continue
                    self._raise_bitrix_error(data)

                if self._should_retry(response, data) and attempt < self.settings.max_retries:
                    delay = self.settings.retry_base_delay * (2**attempt)
                    logger.warning(
                        "Retry %s attempt %s, delay %.1fs, webhook=%s",
                        method,
                        attempt + 1,
                        delay,
                        mask_webhook(self.base_url),
                    )
                    time.sleep(delay)
                    continue

                response.raise_for_status()
                return data

            except (BitrixAuthenticationError, BitrixRateLimitError):
                raise
            except ExportCancelledError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.settings.max_retries:
                    delay = self.settings.retry_base_delay * (2**attempt)
                    logger.warning("HTTP error on %s: %s, retry in %.1fs", method, exc, delay)
                    time.sleep(delay)
                    continue
                raise BitrixAPIError(str(exc)) from exc

        raise BitrixAPIError(str(last_exc) if last_exc else "Неизвестная ошибка Bitrix24")

    def get_paginated(self, method: str, params: dict, limit: int | None = None) -> list[dict]:
        results: list[dict] = []
        start = 0
        page_params = dict(params)
        page_params.setdefault("order", {"ID": "ASC"})

        while True:
            if self.cancel_check():
                raise ExportCancelledError()
            page_params["start"] = start
            data = self.call(method, page_params)
            batch = data.get("result", [])
            if batch is None:
                batch = []
            if not isinstance(batch, list):
                batch = [batch] if batch else []
            results.extend(batch)
            if limit is not None and len(results) >= limit:
                return results[:limit]
            nxt = data.get("next")
            if nxt is None or not batch:
                break
            start = nxt
        return results

    def batch(self, commands: list[tuple[str, dict]], key_prefix: str = "cmd") -> dict[str, Any]:
        if not commands:
            return {}
        results: dict[str, Any] = {}
        for i in range(0, len(commands), BATCH_SIZE):
            if self.cancel_check():
                raise ExportCancelledError()
            chunk = commands[i : i + BATCH_SIZE]
            cmd_dict = {
                f"{key_prefix}{i + j}": f"{method}?{self._encode_params(params)}"
                for j, (method, params) in enumerate(chunk)
            }
            try:
                data = self.call("batch", {"halt": 0, "cmd": cmd_dict})
                result_block = data.get("result", {})
                sub = result_block.get("result", {}) if isinstance(result_block, dict) else {}
                if isinstance(sub, dict):
                    for key, value in sub.items():
                        results[key] = value
                elif isinstance(sub, list):
                    for j, value in enumerate(sub):
                        results[f"{key_prefix}{i + j}"] = value
            except BitrixAPIError:
                logger.info("Batch failed, falling back to sequential calls")
                for j, (method, params) in enumerate(chunk):
                    results[f"{key_prefix}{i + j}"] = self.call(method, params).get("result")
        return results

    @staticmethod
    def _encode_params(params: dict) -> str:
        from urllib.parse import urlencode

        flat: list[tuple[str, str]] = []

        def flatten(prefix: str, obj: Any) -> None:
            if isinstance(obj, dict):
                for k, v in obj.items():
                    flatten(f"{prefix}[{k}]" if prefix else str(k), v)
            elif isinstance(obj, list):
                for idx, v in enumerate(obj):
                    flatten(f"{prefix}[{idx}]", v)
            else:
                flat.append((prefix, str(obj)))

        flatten("", params)
        return urlencode(flat)

    def test_connection(self) -> bool:
        data = self.call("profile")
        return "result" in data

    def get_categories(self) -> list[dict[str, Any]]:
        data = self.call("crm.category.list", {"entityTypeId": 2})
        categories = data.get("result", {}).get("categories", [])
        if not categories:
            return [{"id": 0, "name": "Общая"}]
        result = [{"id": 0, "name": "Общая"}]
        for cat in categories:
            result.append({"id": int(cat.get("id", 0)), "name": cat.get("name", "")})
        return result

    def get_stages(self, category_id: int) -> list[dict[str, Any]]:
        entity_id = "DEAL_STAGE" if category_id == 0 else f"DEAL_STAGE_{category_id}"
        stages = self.get_paginated(
            "crm.status.list",
            {"filter": {"ENTITY_ID": entity_id}, "order": {"SORT": "ASC"}},
        )
        return [
            {
                "id": s.get("STATUS_ID", ""),
                "name": s.get("NAME", ""),
                "category_id": category_id,
            }
            for s in stages
            if s.get("STATUS_ID")
        ]

    def get_lead_statuses(self) -> list[dict[str, Any]]:
        stages = self.get_paginated(
            "crm.status.list",
            {"filter": {"ENTITY_ID": "STATUS"}, "order": {"SORT": "ASC"}},
        )
        return [
            {"id": s.get("STATUS_ID", ""), "name": s.get("NAME", "")}
            for s in stages
            if s.get("STATUS_ID")
        ]

    def get_users(self) -> list[dict[str, Any]]:
        users = self.get_paginated("user.get", {"ACTIVE": True})
        result = []
        for u in users:
            uid = int(u.get("ID", 0))
            name = self.format_user_name(u)
            result.append({"id": uid, "name": name})
            self.users_cache[uid] = u
        result.sort(key=lambda x: x["name"])
        return result

    def find_regions(self, region_name: str, iblock_id: int) -> list[dict[str, Any]]:
        data = self.call(
            "lists.element.get",
            {
                "IBLOCK_TYPE_ID": "lists",
                "IBLOCK_ID": iblock_id,
                "FILTER": {"NAME": region_name},
            },
        )
        elements = data.get("result", []) or []
        return [{"id": int(el.get("ID", 0)), "name": el.get("NAME", "")} for el in elements if el.get("ID")]

    def list_regions(self, iblock_id: int = 49) -> list[dict[str, Any]]:
        elements = self.get_paginated(
            "lists.element.get",
            {
                "IBLOCK_TYPE_ID": "lists",
                "IBLOCK_ID": iblock_id,
                "order": {"NAME": "ASC"},
            },
        )
        result = [
            {"id": int(el.get("ID", 0)), "name": el.get("NAME", "")}
            for el in elements
            if el.get("ID")
        ]
        result.sort(key=lambda x: x["name"])
        return result

    def get_entity_fields(self, entity_type: str) -> dict[str, str]:
        method = ENTITY_FIELD_METHODS.get(entity_type)
        if not method:
            raise ValueError(f"Unknown entity type: {entity_type}")
        data = self.call(method)
        fields = data.get("result", {}) or {}
        result: dict[str, str] = {}
        for code, meta in fields.items():
            if isinstance(meta, dict):
                result[code] = meta.get("title") or meta.get("listLabel") or code
            else:
                result[code] = code
        return result

    def list_deal_ids(
        self,
        category_id: int,
        limit: int,
        excluded_user_ids: list[int] | None = None,
    ) -> list[int]:
        deal_filter: dict[str, Any] = {"CATEGORY_ID": category_id}
        if excluded_user_ids:
            deal_filter["!ASSIGNED_BY_ID"] = excluded_user_ids
        params = {
            "select": ["ID"],
            "filter": deal_filter,
            "order": {"ID": "ASC"},
        }
        deals = self.get_paginated("crm.deal.list", params, limit=limit)
        return [int(d["ID"]) for d in deals if d.get("ID")]

    def batch_get(
        self,
        method: str,
        ids: list[int],
        id_param: str = "id",
    ) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        if not ids:
            return results
        commands = [(method, {id_param: entity_id}) for entity_id in ids]
        batch_results = self.batch(commands)
        for idx, entity_id in enumerate(ids):
            value = batch_results.get(f"cmd{idx}")
            if isinstance(value, dict):
                results[entity_id] = value
        return results

    def batch_deal_contacts(self, deal_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
        results: dict[int, list[dict[str, Any]]] = {}
        if not deal_ids:
            return results
        commands = [("crm.deal.contact.items.get", {"id": deal_id}) for deal_id in deal_ids]
        batch_results = self.batch(commands)
        for idx, deal_id in enumerate(deal_ids):
            items = batch_results.get(f"cmd{idx}") or []
            if not isinstance(items, list):
                items = [items] if items else []
            results[deal_id] = items
            self.deal_contacts_cache[deal_id] = [
                int(i.get("CONTACT_ID", 0)) for i in items if i.get("CONTACT_ID")
            ]
        return results

    def get_deals(
        self,
        deal_filter: dict,
        select: list[str],
        limit: int | None = None,
        region_field: str | None = None,
    ) -> list[dict[str, Any]]:
        fields = list(select)
        if region_field and region_field not in fields:
            fields.append(region_field)
        params = {
            "select": fields,
            "filter": deal_filter,
            "order": {"ID": "ASC"},
        }
        return self.get_paginated("crm.deal.list", params, limit=limit)

    def get_deal_contacts(self, deal_id: int) -> list[int]:
        if deal_id in self.deal_contacts_cache:
            return self.deal_contacts_cache[deal_id]
        data = self.call("crm.deal.contact.items.get", {"id": deal_id})
        items = data.get("result", []) or []
        ids = [int(i.get("CONTACT_ID", 0)) for i in items if i.get("CONTACT_ID")]
        self.deal_contacts_cache[deal_id] = ids
        return ids

    def get_company_contacts(self, company_id: int) -> list[int]:
        if company_id in self.company_contacts_cache:
            return self.company_contacts_cache[company_id]
        try:
            data = self.call("crm.company.contact.items.get", {"id": company_id})
            items = data.get("result", []) or []
            ids = [int(i.get("CONTACT_ID", 0)) for i in items if i.get("CONTACT_ID")]
        except BitrixAPIError:
            contacts = self.get_paginated(
                "crm.contact.list",
                {"filter": {"COMPANY_ID": company_id}, "select": ["ID"]},
            )
            ids = [int(c.get("ID", 0)) for c in contacts if c.get("ID")]
        self.company_contacts_cache[company_id] = ids
        return ids

    def get_contact(self, contact_id: int) -> dict[str, Any] | None:
        if contact_id in self.contacts_cache:
            return self.contacts_cache[contact_id]
        data = self.call("crm.contact.get", {"id": contact_id})
        contact = data.get("result")
        if contact:
            self.contacts_cache[contact_id] = contact
        return contact

    def get_company(self, company_id: int) -> dict[str, Any] | None:
        if company_id in self.companies_cache:
            return self.companies_cache[company_id]
        data = self.call("crm.company.get", {"id": company_id})
        company = data.get("result")
        if company:
            self.companies_cache[company_id] = company
        return company

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        if user_id in self.users_cache:
            return self.users_cache[user_id]
        data = self.call("user.get", {"ID": user_id})
        users = data.get("result", [])
        if users:
            self.users_cache[user_id] = users[0]
            return users[0]
        return None

    @staticmethod
    def format_user_name(user: dict[str, Any]) -> str:
        last = user.get("LAST_NAME", "")
        first = user.get("NAME", "")
        name = f"{last} {first}".strip()
        return name or user.get("LOGIN", "Без имени")

    @staticmethod
    def format_contact_name(contact: dict[str, Any]) -> str:
        parts = [
            contact.get("LAST_NAME", ""),
            contact.get("NAME", ""),
            contact.get("SECOND_NAME", ""),
        ]
        name = " ".join(p for p in parts if p).strip()
        return name or "Без имени"

    def prefetch_contacts_batch(self, contact_ids: list[int]) -> None:
        missing = [cid for cid in contact_ids if cid not in self.contacts_cache]
        if not missing:
            return
        commands = [("crm.contact.get", {"id": cid}) for cid in missing[:BATCH_SIZE]]
        results = self.batch(commands)
        for key, value in results.items():
            if isinstance(value, dict) and value.get("ID"):
                self.contacts_cache[int(value["ID"])] = value

    def prefetch_companies_batch(self, company_ids: list[int]) -> None:
        missing = [cid for cid in company_ids if cid not in self.companies_cache]
        if not missing:
            return
        commands = [("crm.company.get", {"id": cid}) for cid in missing[:BATCH_SIZE]]
        results = self.batch(commands)
        for key, value in results.items():
            if isinstance(value, dict) and value.get("ID"):
                self.companies_cache[int(value["ID"])] = value
