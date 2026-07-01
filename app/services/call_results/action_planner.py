"""Build Bitrix REST actions from business signals."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from app.models import CallResultImportRow
from app.services.call_results.llm_schema import CallResultSignals


@dataclass
class PlannedAction:
    method: str
    action_type: str
    operation_type: str
    payload: dict[str, Any]
    human_summary: str
    validation_status: str = "valid"
    validation_errors: list[str] = field(default_factory=list)
    is_enabled: bool = True
    sort_order: int = 0


class BitrixActionPlanner:
    """Signal-based action planner v2."""

    def plan(
        self,
        row: CallResultImportRow,
        *,
        bitrix_deal_id: int | None,
        assigned_by_id: int | None,
        signals: CallResultSignals,
        requires_manual: bool,
        contact_creation_allowed: bool = True,
    ) -> list[PlannedAction]:
        if requires_manual or signals.needs_manual_review:
            return [
                PlannedAction(
                    method="manual_review.required",
                    action_type="manual_review_required",
                    operation_type="manual_review_required",
                    payload={"reason": signals.manual_review_reason},
                    human_summary="Требуется ручная проверка",
                    is_enabled=False,
                    sort_order=0,
                )
            ]

        if row.match_status in ("ambiguous", "conflict", "not_found", "invalid"):
            return []

        if not bitrix_deal_id:
            return []

        actions: list[PlannedAction] = []
        order = 0

        if signals.positive:
            if not assigned_by_id:
                return [
                    PlannedAction(
                        method="manual_review.required",
                        action_type="manual_review_required",
                        operation_type="manual_review_required",
                        payload={"reason": "Нет ответственного по сделке"},
                        human_summary="Нет ответственного — ручная проверка",
                        is_enabled=False,
                        sort_order=0,
                    )
                ]
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.activity.todo.add",
                    action_type="crm_todo",
                    operation_type="bitrix_add_todo",
                    payload={},
                    human_summary="CRM-дело: положительный результат обзвона",
                    sort_order=order,
                ),
            )

        if signals.alternate_contact_requested:
            ac = signals.alternate_contact
            phone = ac.phone
            if not phone or len("".join(c for c in str(phone) if c.isdigit())) < 10:
                return [
                    PlannedAction(
                        method="manual_review.required",
                        action_type="manual_review_required",
                        operation_type="manual_review_required",
                        payload={"reason": "Нет полного валидного телефона нового контакта"},
                        human_summary="Неполный телефон — ручная проверка",
                        is_enabled=False,
                        sort_order=0,
                    )
                ]
            if not contact_creation_allowed:
                return [
                    PlannedAction(
                        method="manual_review.required",
                        action_type="manual_review_required",
                        operation_type="manual_review_required",
                        payload={"reason": "Признак контакта не настроен в Bitrix"},
                        human_summary="Создание контакта отключено",
                        is_enabled=False,
                        sort_order=0,
                    )
                ]
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.contact.list",
                    action_type="bitrix_find_contact",
                    operation_type="bitrix_find_contact",
                    payload={"phone": phone},
                    human_summary="Поиск контакта по телефону",
                    sort_order=order,
                ),
            )
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.contact.add",
                    action_type="bitrix_create_contact",
                    operation_type="bitrix_create_contact",
                    payload={"contact": ac.model_dump()},
                    human_summary="Создание или обновление контакта",
                    sort_order=order,
                ),
            )
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.deal.contact.add",
                    action_type="bitrix_link_contact_to_deal",
                    operation_type="bitrix_link_contact_to_deal",
                    payload={"deal_id": bitrix_deal_id, "is_primary": "N"},
                    human_summary="Привязка контакта к сделке",
                    sort_order=order,
                ),
            )
            order = self._append(
                actions,
                PlannedAction(
                    method="retry_queue.add",
                    action_type="retry_queue_add",
                    operation_type="retry_queue_add",
                    payload={"reason": "alternate_contact"},
                    human_summary="Добавить в очередь повторных звонков",
                    sort_order=order,
                ),
            )

        if signals.callback_later_requested and not signals.alternate_contact_requested:
            order = self._append(
                actions,
                PlannedAction(
                    method="retry_queue.add",
                    action_type="retry_queue_add",
                    operation_type="retry_queue_add",
                    payload={"reason": "callback_later"},
                    human_summary="Перезвонить позже — очередь повторов",
                    sort_order=order,
                ),
            )
        elif signals.callback_later_requested and signals.alternate_contact_requested:
            # retry already added with alternate contact phone
            pass

        if (
            signals.no_answer
            and not signals.alternate_contact_requested
            and not signals.callback_later_requested
        ):
            order = self._append(
                actions,
                PlannedAction(
                    method="retry_queue.add",
                    action_type="retry_queue_add",
                    operation_type="retry_queue_add",
                    payload={"reason": "no_answer"},
                    human_summary="Не дозвонились — очередь повторов",
                    sort_order=order,
                ),
            )

        if signals.explicit_refusal:
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.timeline.comment.add",
                    action_type="timeline_comment",
                    operation_type="bitrix_add_comment",
                    payload={},
                    human_summary="Комментарий: отказ клиента",
                    sort_order=order,
                ),
            )

        if signals.hangup_without_result and signals.active_signal_count() == 1:
            # TODO(contact-search): populate deal_contact_ids from local CRM / Bitrix API
            order = self._append(
                actions,
                PlannedAction(
                    method="contact_search.add",
                    action_type="contact_search_queue_add",
                    operation_type="contact_search_queue_add",
                    payload={"deal_contact_ids": []},
                    human_summary="Требуется поиск нового контакта",
                    sort_order=order,
                ),
            )
            order = self._append(
                actions,
                PlannedAction(
                    method="retry_queue.add",
                    action_type="retry_queue_add",
                    operation_type="retry_queue_add",
                    payload={
                        "reason": "hangup_replacement_contact",
                        "search_required": True,
                    },
                    human_summary="Перезвон на другой номер — ожидает поиск контакта",
                    sort_order=order,
                ),
            )

        group_id = str(uuid.uuid4())
        for a in actions:
            a.payload["_group_id"] = group_id
        return actions

    @staticmethod
    def _append(actions: list[PlannedAction], action: PlannedAction) -> int:
        actions.append(action)
        return action.sort_order + 1
