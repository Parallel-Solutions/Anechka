"""Build Bitrix REST API payloads (v2 signal-based)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.models import CallResultImportRow
from app.services.call_results.action_planner import PlannedAction


def parse_positive_deadline(settings: Settings) -> timedelta:
    raw = getattr(settings, "positive_activity_default_deadline", "24h") or "24h"
    raw = str(raw).strip().lower()
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1] or 24))
    if raw.endswith("d"):
        return timedelta(days=int(raw[:-1] or 1))
    return timedelta(hours=24)


class BitrixPayloadBuilder:
    def build(
        self,
        action: PlannedAction,
        row: CallResultImportRow,
        *,
        bitrix_deal_id: int,
        assigned_by_id: int | None,
        service_user_id: int,
        campaign_label: str = "",
        comment_override: str | None = None,
        todo_title: str | None = None,
        todo_description: str | None = None,
        deadline: datetime | None = None,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        ext = row.extracted_data or {}
        sig = row.business_signals or {}

        if action.method == "crm.timeline.comment.add":
            return self._comment_payload(
                row, bitrix_deal_id, campaign_label, comment_override, ext, sig
            )
        if action.method == "crm.activity.todo.add":
            return self._todo_payload(
                row,
                bitrix_deal_id,
                assigned_by_id,
                todo_title,
                todo_description,
                deadline,
                ext,
                sig,
                settings,
            )
        return action.payload

    def _comment_payload(
        self,
        row: CallResultImportRow,
        deal_id: int,
        campaign: str,
        override: str | None,
        ext: dict,
        sig: dict,
    ) -> dict:
        if override:
            comment = override
        else:
            lines = [
                "Результат автоматического обзвона",
                "",
                "Категория: Отказ",
            ]
            if row.called_at:
                lines.append(f"Дата звонка: {row.called_at.isoformat()}")
            if row.raw_phone:
                lines.append(f"Телефон: {row.raw_phone}")
            contact = ext.get("contact_name") or row.normalized_data.get("contact_name")
            if contact:
                lines.append(f"Контакт: {contact}")
            reason = sig.get("refusal_reason") or ext.get("refusal_reason")
            if reason:
                lines.append(f"Причина отказа: {reason}")
            summary = sig.get("summary") or ext.get("summary") or row.comment
            if summary:
                lines.extend(["", "Краткое резюме:", str(summary)])
            if row.call_id:
                lines.append(f"Call ID: {row.call_id}")
            if row.campaign_id or campaign:
                lines.append(f"Campaign ID: {row.campaign_id or campaign}")
            rec = row.normalized_data.get("recording_url")
            if rec:
                lines.append(f"Запись: {rec}")
            comment = "\n".join(lines)
        return {"fields": {"ENTITY_ID": deal_id, "ENTITY_TYPE": "deal", "COMMENT": comment}}

    def _todo_payload(
        self,
        row: CallResultImportRow,
        deal_id: int,
        responsible_id: int | None,
        title: str | None,
        description: str | None,
        deadline: datetime | None,
        ext: dict,
        sig: dict,
        settings: Settings | None,
    ) -> dict:
        summary = sig.get("summary") or ext.get("summary") or row.comment or ""
        desc_parts = [
            description or "Краткий итог разговора:",
            summary[:800] if summary else "",
        ]
        if row.raw_phone:
            desc_parts.append(f"Исходный телефон: {row.raw_phone}")
        contact = ext.get("contact_name")
        if contact:
            desc_parts.append(f"Контакт: {contact}")
        cb = row.callback_at or sig.get("callback_at")
        if cb:
            desc_parts.append(f"Запрошенный перезвон: {cb}")
        desc_parts.append("Источник: автоматический обзвон «Анечка»")
        if row.call_id:
            desc_parts.append(f"Call ID: {row.call_id}")
        if row.campaign_id:
            desc_parts.append(f"Campaign ID: {row.campaign_id}")
        rec = row.normalized_data.get("recording_url")
        if rec:
            desc_parts.append(f"Запись: {rec}")

        dl = deadline or row.callback_at
        if dl is None and settings is not None:
            dl = datetime.now(tz=row.called_at.tzinfo if row.called_at else None) + parse_positive_deadline(settings)

        payload = {
            "ownerTypeId": 2,
            "ownerId": deal_id,
            "title": title or "Обработать положительный результат обзвона",
            "description": "\n".join(p for p in desc_parts if p),
            "pingOffsets": [0, 15],
        }
        if responsible_id:
            payload["responsibleId"] = responsible_id
        if dl:
            payload["deadline"] = dl.isoformat() if hasattr(dl, "isoformat") else str(dl)
        return payload
