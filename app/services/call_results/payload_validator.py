"""Validate prepared Bitrix payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PayloadValidation:
    status: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class BitrixPayloadValidator:
    FORBIDDEN_METHODS = frozenset({
        "tasks.task.add",
        "bitrix_archive_deal",
        "crm.item.update",
    })

    def validate(self, method: str, payload: dict[str, Any]) -> PayloadValidation:
        if method in self.FORBIDDEN_METHODS:
            return PayloadValidation(status="invalid", errors=[f"Запрещённый метод: {method}"])

        errors: list[str] = []
        warnings: list[str] = []

        if method == "crm.timeline.comment.add":
            fields = payload.get("fields") or payload
            if not fields.get("ENTITY_ID"):
                errors.append("ENTITY_ID обязателен")
            if not fields.get("COMMENT"):
                errors.append("COMMENT обязателен")
            if fields.get("ENTITY_TYPE") and fields.get("ENTITY_TYPE") != "deal":
                warnings.append("ENTITY_TYPE должен быть deal")

        elif method == "crm.activity.todo.add":
            if not payload.get("ownerId"):
                errors.append("ownerId обязателен")
            if not payload.get("title"):
                errors.append("title обязателен")
            if not payload.get("responsibleId"):
                errors.append("responsibleId обязателен")
            if not payload.get("deadline"):
                warnings.append("deadline не указан")

        elif method in ("crm.contact.add", "crm.contact.update"):
            if not payload.get("fields") and not payload.get("contact"):
                errors.append("Нет данных контакта")

        elif method == "crm.deal.contact.add":
            if not payload.get("deal_id") and not payload.get("id"):
                errors.append("deal_id обязателен")

        elif method in ("retry_queue.add", "contact_search.add", "manual_review.required"):
            pass

        elif method == "crm.contact.list":
            if not payload.get("phone"):
                errors.append("phone обязателен для поиска")

        if errors:
            return PayloadValidation(status="invalid", errors=errors, warnings=warnings)
        if warnings:
            return PayloadValidation(status="warning", warnings=warnings)
        return PayloadValidation(status="valid")
