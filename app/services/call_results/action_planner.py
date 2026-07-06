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
        resolved_alternate_contact_id: int | None = None,
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
            order = self._append(
                actions,
                PlannedAction(
                    method="tasks.task.add",
                    action_type="task",
                    operation_type="bitrix_add_task",
                    payload={},
                    human_summary="Задача: положительный результат обзвона",
                    sort_order=order,
                ),
            )

        if signals.alternate_contact_requested:
            if resolved_alternate_contact_id:
                order = self._append_alternate_contact_found(
                    actions,
                    bitrix_deal_id=bitrix_deal_id,
                    contact_id=resolved_alternate_contact_id,
                    sort_order=order,
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
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.timeline.comment.add",
                    action_type="timeline_comment",
                    operation_type="bitrix_add_comment",
                    payload={},
                    human_summary="Комментарий: сброс трубки без результата",
                    sort_order=order,
                ),
            )

        if signals.replacement_contact_required and not signals.hangup_without_result:
            phone = row.normalized_phone or row.raw_phone
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
            created = self.plan_manual_create_contact(
                str(phone or ""),
                deal_id=bitrix_deal_id,
                contact_creation_allowed=contact_creation_allowed,
            )
            for pa in created:
                pa.sort_order = order
                order = self._append(actions, pa)

        group_id = str(uuid.uuid4())
        for a in actions:
            a.payload["_group_id"] = group_id
        return actions

    def plan_alternate_contact_found(
        self,
        *,
        bitrix_deal_id: int,
        contact_id: int,
        sort_order: int = 0,
    ) -> list[PlannedAction]:
        actions: list[PlannedAction] = []
        order = self._append_alternate_contact_found(
            actions,
            bitrix_deal_id=bitrix_deal_id,
            contact_id=contact_id,
            sort_order=sort_order,
        )
        group_id = str(uuid.uuid4())
        for a in actions:
            a.payload["_group_id"] = group_id
        return actions

    def _append_alternate_contact_found(
        self,
        actions: list[PlannedAction],
        *,
        bitrix_deal_id: int,
        contact_id: int,
        sort_order: int,
    ) -> int:
        order = self._append(
            actions,
            PlannedAction(
                method="crm.deal.contact.add",
                action_type="bitrix_link_contact_to_deal",
                operation_type="bitrix_link_contact_to_deal",
                payload={"deal_id": bitrix_deal_id, "is_primary": "N", "contact_id": contact_id},
                human_summary="Привязка контакта к сделке",
                sort_order=sort_order,
            ),
        )
        order = self._append(
            actions,
            PlannedAction(
                method="retry_queue.add",
                action_type="retry_queue_add",
                operation_type="retry_queue_add",
                payload={"reason": "alternate_contact", "contact_id": contact_id},
                human_summary="Добавить в очередь повторных звонков",
                sort_order=order,
            ),
        )
        return order

    def plan_manual_create_contact(
        self,
        phone: str,
        *,
        deal_id: int | None,
        contact_creation_allowed: bool = True,
        contact_data: dict[str, Any] | None = None,
    ) -> list[PlannedAction]:
        if not contact_creation_allowed:
            return []
        digits = "".join(c for c in str(phone) if c.isdigit())
        if len(digits) < 10:
            return []

        cd = contact_data or {}
        contact_payload = {
            "name": cd.get("name"),
            "email": cd.get("email"),
            "phone": cd.get("phone") or phone,
            "position": cd.get("position"),
            "extension": cd.get("extension"),
        }
        actions: list[PlannedAction] = []
        order = 0
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
                payload={"contact": contact_payload},
                human_summary="Создание контакта по телефону звонка",
                sort_order=order,
            ),
        )
        if deal_id:
            order = self._append(
                actions,
                PlannedAction(
                    method="crm.deal.contact.add",
                    action_type="bitrix_link_contact_to_deal",
                    operation_type="bitrix_link_contact_to_deal",
                    payload={"deal_id": deal_id, "is_primary": "N"},
                    human_summary="Привязка контакта к сделке",
                    sort_order=order,
                ),
            )

        group_id = str(uuid.uuid4())
        for a in actions:
            a.payload["_group_id"] = group_id
        return actions

    def plan_outcome_comment(self, *, sort_order: int) -> PlannedAction:
        return PlannedAction(
            method="crm.timeline.comment.add",
            action_type="timeline_comment",
            operation_type="bitrix_add_comment",
            payload={},
            human_summary="Комментарий: результат обзвона",
            sort_order=sort_order,
        )

    @staticmethod
    def _append(actions: list[PlannedAction], action: PlannedAction) -> int:
        actions.append(action)
        return action.sort_order + 1
