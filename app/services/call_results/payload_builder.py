"""Build Bitrix REST API payloads (v2 signal-based)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.models import CallResultImportRow
from app.services.call_results.action_planner import PlannedAction
from app.services.call_results.row_disposition import row_matches_manual_call


COMMENT_CATEGORY_LABELS: dict[str, str] = {
    "refusal": "Отказ",
    "positive": "Положительный результат",
    "callback_later": "Запрос перезвона",
    "alternate_contact": "Просьба перезвонить, дан другой контакт",
    "hangup": "Сброс / нет результата",
    "hangup_during_robocall": "Бросил без разговора",
}

HANGUP_DURING_ROBO_SUMMARY = "дозвон был, человек бросил трубку"


def comment_category_label(row: CallResultImportRow) -> str:
    outcome = row.primary_outcome or ""
    if outcome in COMMENT_CATEGORY_LABELS:
        return COMMENT_CATEGORY_LABELS[outcome]
    sig = row.business_signals or {}
    if sig.get("explicit_refusal"):
        return COMMENT_CATEGORY_LABELS["refusal"]
    if sig.get("positive"):
        return COMMENT_CATEGORY_LABELS["positive"]
    if sig.get("callback_later_requested"):
        return COMMENT_CATEGORY_LABELS["callback_later"]
    if sig.get("alternate_contact_requested"):
        return COMMENT_CATEGORY_LABELS["alternate_contact"]
    if sig.get("hangup_during_robocall"):
        return COMMENT_CATEGORY_LABELS["hangup_during_robocall"]
    if sig.get("hangup_without_result"):
        return COMMENT_CATEGORY_LABELS["hangup"]
    return "Результат обзвона"


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
        context_actions: list | None = None,
    ) -> dict[str, Any]:
        ext = row.extracted_data or {}
        sig = row.business_signals or {}

        if action.method == "crm.timeline.comment.add":
            return self._comment_payload(
                row,
                bitrix_deal_id,
                campaign_label,
                comment_override,
                ext,
                sig,
                context_actions=context_actions,
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
        if action.method == "tasks.task.add":
            return self._task_payload(
                row,
                bitrix_deal_id,
                title=todo_title,
                description=todo_description,
                deadline=deadline,
                ext=ext,
                sig=sig,
                settings=settings,
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
        *,
        context_actions: list | None = None,
    ) -> dict:
        if override:
            comment = override
        else:
            category = comment_category_label(row)
            lines = ["Результат автоматического обзвона"]
            if row_matches_manual_call(row, context_actions or []):
                lines.extend(["", "Рекомендуется к ручному обзвону"])
            lines.extend(["", f"Категория: {category}"])
            if row.called_at:
                lines.append(f"Дата звонка: {row.called_at.isoformat()}")
            if row.raw_phone:
                lines.append(f"Телефон: {row.raw_phone}")
            contact = ext.get("contact_name") or row.normalized_data.get("contact_name")
            if contact:
                lines.append(f"Контакт: {contact}")
            cb = row.callback_at or sig.get("callback_at")
            if cb and category == COMMENT_CATEGORY_LABELS["callback_later"]:
                lines.append(f"Запрошенный перезвон: {cb}")
            reason = sig.get("refusal_reason") or ext.get("refusal_reason")
            if reason and category == COMMENT_CATEGORY_LABELS["refusal"]:
                lines.append(f"Причина отказа: {reason}")
            summary = sig.get("summary") or ext.get("summary") or row.comment
            if sig.get("hangup_during_robocall"):
                summary = HANGUP_DURING_ROBO_SUMMARY
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
        if dl:
            payload["deadline"] = dl.isoformat() if hasattr(dl, "isoformat") else str(dl)
        return payload

    def _task_payload(
        self,
        row: CallResultImportRow,
        deal_id: int,
        *,
        title: str | None,
        description: str | None,
        deadline: datetime | None,
        ext: dict,
        sig: dict,
        settings: Settings | None,
    ) -> dict:
        outcome_label = comment_category_label(row)
        summary = sig.get("summary") or ext.get("summary") or row.comment or ""
        contact = ext.get("contact_name") or row.normalized_data.get("contact_name")
        manager_action = summary[:800] if summary else "Обработать положительный результат обзвона"

        desc_parts = [
            description or "Контекст звонка:",
            f"Результат звонка: {outcome_label}",
        ]
        if contact:
            desc_parts.append(f"Данные клиента: {contact}")
        if row.raw_phone:
            desc_parts.append(f"Телефон: {row.raw_phone}")
        if row.comment:
            desc_parts.append(f"Комментарий клиента: {row.comment}")
        desc_parts.append(f"ID сделки: {deal_id}")
        if row.called_at:
            desc_parts.append(f"Дата и время звонка: {row.called_at.isoformat()}")
        desc_parts.append(f"Что сделать менеджеру: {manager_action}")
        if summary and summary != manager_action:
            desc_parts.append(f"Краткое резюме: {summary[:800]}")
        cb = row.callback_at or sig.get("callback_at")
        if cb:
            desc_parts.append(f"Запрошенный перезвон: {cb}")
        desc_parts.append("Источник: автоматический обзвон «Анечка»")
        if row.call_id:
            desc_parts.append(f"Call ID: {row.call_id}")

        dl = deadline or row.callback_at
        if dl is None and settings is not None:
            dl = datetime.now(tz=row.called_at.tzinfo if row.called_at else None) + parse_positive_deadline(settings)

        fields: dict[str, Any] = {
            "TITLE": title or "Обработать положительный результат обзвона",
            "DESCRIPTION": "\n".join(p for p in desc_parts if p),
            "UF_CRM_TASK": [f"D_{deal_id}"],
        }
        if dl:
            fields["DEADLINE"] = dl.isoformat() if hasattr(dl, "isoformat") else str(dl)
        return {"fields": fields}
