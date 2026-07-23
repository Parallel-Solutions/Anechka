"""Execute prepared Bitrix actions with idempotency."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import BitrixPreparedAction, CallResultImport, CallResultImportRow, utcnow
from app.repositories.call_result_repository import CallResultRepository
from app.services.call_results.bitrix_gateway import CallResultsBitrixGateway, build_bitrix_gateway
from app.services.call_results.contact_marker_validator import ContactMarkerValidator
from app.services.call_results.contact_search_gateway import ContactSearchGateway
from app.services.call_results.fake_bitrix_gateway import FakeBitrixGateway
from app.services.call_results.payload_validator import BitrixPayloadValidator
from app.services.call_results.retry_queue_gateway import RetryQueueGateway
from app.services.call_results.task_responsible import (
    resolve_task_creator_user,
    resolve_task_responsible_user,
)
from app.services.security_service import mask_webhook

logger = logging.getLogger(__name__)

FORBIDDEN_METHODS = frozenset({"bitrix_archive_deal", "crm.item.update"})


class CrmActionService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        portal_id: str,
        gateway: CallResultsBitrixGateway | None = None,
    ):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id
        self.repo = CallResultRepository(db, portal_id)
        self.validator = BitrixPayloadValidator()
        self.retry_gw = RetryQueueGateway(db, portal_id)
        self.search_gw = ContactSearchGateway(db, portal_id, settings=settings)
        self.marker = ContactMarkerValidator(settings)
        if gateway is not None:
            self.gateway = gateway
        elif settings.call_results_bitrix_execution_enabled and settings.bitrix_webhook_url:
            self.gateway = build_bitrix_gateway(settings)
        else:
            self.gateway = FakeBitrixGateway()

        self._ctx: dict[str, Any] = {}
        self._responsible_user_id: int | None = None

    def execute_import(
        self,
        import_id: int,
        *,
        row_ids: list[int] | None = None,
        retry_failed_only: bool = False,
        responsible_user_id: int | None = None,
    ) -> dict[str, Any]:
        imp = self.repo.get_import(import_id)
        if imp is None:
            raise ValueError("Import not found")

        if not self.settings.call_results_bitrix_execution_enabled:
            raise PermissionError("CALL_RESULTS_BITRIX_EXECUTION_ENABLED=false")

        imp.execute_status = "executing"
        imp.execute_started_at = utcnow()
        self.db.commit()

        self._responsible_user_id = responsible_user_id
        stats = {"succeeded": 0, "failed": 0, "skipped": 0, "blocked": 0}
        rows = self.repo.list_rows(import_id)
        if row_ids:
            id_set = set(row_ids)
            rows = [r for r in rows if r.id in id_set]

        for row in rows:
            if row.needs_manual_review or row.execution_status == "blocked_manual_review":
                stats["blocked"] += 1
                continue
            row_stats = self.execute_row(row, imp, retry_failed_only=retry_failed_only)
            for k in stats:
                stats[k] += row_stats.get(k, 0)

        imp.execute_status = "completed" if stats["failed"] == 0 else "partial"
        imp.execute_completed_at = utcnow()
        self.db.commit()
        return stats

    def execute_row(
        self,
        row: CallResultImportRow,
        imp: CallResultImport,
        *,
        retry_failed_only: bool = False,
    ) -> dict[str, int]:
        stats = {"succeeded": 0, "failed": 0, "skipped": 0, "blocked": 0}
        if row.needs_manual_review:
            row.execution_status = "blocked_manual_review"
            stats["blocked"] += 1
            return stats

        actions = sorted(
            [a for a in self.repo.list_actions(imp.id) if a.import_row_id == row.id],
            key=lambda a: (a.sort_order, a.id),
        )
        self._ctx = {"contact_id": None, "deal_id": row.matched_deal_id}
        row.execution_status = "executing"

        for action in actions:
            if action.method in FORBIDDEN_METHODS:
                action.execution_status = "skipped"
                action.is_enabled = False
                stats["skipped"] += 1
                continue
            if not action.is_enabled or action.validation_status == "invalid":
                action.execution_status = "skipped"
                stats["skipped"] += 1
                continue
            if action.execution_status == "succeeded":
                stats["skipped"] += 1
                continue
            if retry_failed_only and action.execution_status not in ("failed", "prepared"):
                stats["skipped"] += 1
                continue

            pv = self.validator.validate(action.method, action.payload)
            if pv.status == "invalid":
                action.execution_status = "failed"
                action.last_error = "; ".join(pv.errors)
                stats["failed"] += 1
                continue

            action.execution_status = "executing"
            action.attempt_count += 1
            action.started_at = utcnow()
            action.request_payload = action.payload
            self.db.commit()

            ok = self._execute_action(action, row, imp)
            action.completed_at = utcnow()
            if action.execution_status == "skipped":
                stats["skipped"] += 1
            elif ok:
                action.execution_status = "succeeded"
                stats["succeeded"] += 1
            else:
                action.execution_status = "failed"
                stats["failed"] += 1
            self.db.commit()

        row.execution_status = "completed" if stats["failed"] == 0 else "partial"
        return stats

    def _execute_action(
        self,
        action: BitrixPreparedAction,
        row: CallResultImportRow,
        imp: CallResultImport,
    ) -> bool:
        op = action.operation_type or action.action_type
        try:
            if op == "bitrix_add_todo":
                payload, resp_err = self._inject_todo_responsible(dict(action.payload), row)
                if payload is None:
                    action.last_error = resp_err or "Не удалось определить ответственного"
                    return False
                action.request_payload = payload
                res = self.gateway.add_deal_todo(payload)
            elif op == "bitrix_add_task":
                return self._execute_add_task(action, row)
            elif op == "bitrix_add_comment":
                res = self.gateway.add_deal_comment(action.payload)
            elif op == "bitrix_find_contact":
                phone = action.payload.get("phone") or (row.business_signals or {}).get("alternate_contact", {}).get("phone")
                res = self.gateway.find_contact_by_phone(str(phone))
                if res.external_id:
                    self._ctx["contact_id"] = int(res.external_id)
            elif op == "bitrix_create_contact":
                if self._ctx.get("contact_id"):
                    action.execution_status = "skipped"
                    action.response_payload = {"skipped": "contact_exists"}
                    return True
                ac = action.payload.get("contact") or {}
                fields = self._contact_fields(ac, row)
                res = self.gateway.create_contact(fields)
                if res.external_id:
                    self._ctx["contact_id"] = int(res.external_id)
            elif op == "bitrix_update_contact":
                cid = self._ctx.get("contact_id")
                if not cid:
                    action.last_error = "contact_id missing"
                    return False
                fields = self._contact_fields(action.payload.get("contact") or {}, row)
                res = self.gateway.update_contact_missing_fields(int(cid), fields)
            elif op == "bitrix_link_contact_to_deal":
                cid = (
                    action.payload.get("contact_id")
                    or self._ctx.get("contact_id")
                    or row.matched_contact_id
                )
                deal_id = row.matched_deal_id
                if not cid or not deal_id:
                    action.last_error = "deal_id or contact_id missing"
                    return False
                res = self.gateway.link_contact_to_deal(int(deal_id), int(cid))
            elif op == "retry_queue_add":
                reason = action.payload.get("reason", "callback_later")
                search_required = bool(action.payload.get("search_required"))
                phone = None if search_required else row.normalized_phone
                phone_extension = row.phone_extension
                resolved_contact_id = action.payload.get("contact_id") or self._ctx.get("contact_id")
                if resolved_contact_id:
                    ac = (row.business_signals or {}).get("alternate_contact") or {}
                    phone = ac.get("phone") or phone
                    phone_extension = ac.get("extension") or phone_extension
                self.retry_gw.add(
                    import_id=imp.id,
                    row_id=row.id,
                    deal_id=row.matched_deal_id,
                    contact_id=resolved_contact_id or row.matched_contact_id,
                    phone_normalized=self._norm_phone(str(phone)) if phone else None,
                    callback_at=row.callback_at,
                    callback_text=(row.business_signals or {}).get("callback_text"),
                    reason=reason,
                    campaign_id=row.campaign_id,
                    source_call_id=row.call_id,
                    source_contact_id=row.matched_contact_id,
                    replacement_contact_id=self._ctx.get("contact_id"),
                    search_required=search_required,
                    timezone=action.payload.get("timezone"),
                    phone_extension=phone_extension,
                    attempt_count=row.attempts or 0,
                )
                res = type("R", (), {"success": True, "external_id": None, "response": {}, "error": None})()
            elif op == "contact_search_queue_add":
                deal_contacts = action.payload.get("deal_contact_ids") or []
                entry = self.search_gw.create_from_row(row, deal_contact_ids=deal_contacts)
                res = type("R", (), {"success": True, "external_id": str(entry.id), "response": {}, "error": None})()
            elif op == "manual_review_required":
                action.execution_status = "blocked_manual_review"
                return False
            else:
                action.last_error = f"Unknown operation: {op}"
                return False

            action.response_payload = res.response if hasattr(res, "response") else {}
            if res.external_id:
                action.external_id = str(res.external_id)
            if not res.success:
                action.last_error = res.error
                return False
            return True
        except Exception as exc:
            action.last_error = str(exc)
            logger.exception("Action %s failed", action.id)
            return False

    def _contact_fields(self, ac: dict, row: CallResultImportRow) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        name = ac.get("name") or ""
        parts = name.split(maxsplit=1)
        if parts:
            fields["NAME"] = parts[0]
            if len(parts) > 1:
                fields["LAST_NAME"] = parts[1]
        if ac.get("email"):
            fields["EMAIL"] = [{"VALUE": ac["email"], "VALUE_TYPE": "WORK"}]
        phone = ac.get("phone")
        if phone:
            fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
        code = getattr(self.settings, "bitrix_call_source_field_code", "") or ""
        val = getattr(self.settings, "bitrix_call_source_field_value", "") or ""
        if code and val:
            fields[code] = val
        fields["COMMENTS"] = self._contact_comments(ac, row)
        return fields

    def _contact_comments(self, ac: dict, row: CallResultImportRow) -> str:
        sig = row.business_signals or {}
        ext = row.extracted_data or {}
        lines = ["Контакт получен в ходе автоматического обзвона Анечкой"]
        if row.raw_phone:
            lines.append(f"Исходный телефон: {row.raw_phone}")
        if ac.get("phone"):
            lines.append(f"Новый телефон: {ac['phone']}")
        if ac.get("position"):
            lines.append(f"Должность: {ac['position']}")
        if ac.get("extension"):
            lines.append(f"Добавочный: {ac['extension']}")
        if row.called_at:
            lines.append(f"Дата звонка: {row.called_at.isoformat()}")
        cb = row.callback_at or sig.get("callback_at")
        if cb:
            lines.append(f"Запрошенный перезвон: {cb}")
        summary = sig.get("summary") or ext.get("summary") or row.comment
        if summary:
            lines.extend(["", "Краткое резюме:", str(summary)])
        lines.append("")
        lines.append(
            f"source=anechka_call; call_id={row.call_id}; import_id={row.import_id}; row_id={row.id}"
        )
        return "\n".join(lines)

    @staticmethod
    def _norm_phone(phone: str) -> str:
        digits = re.sub(r"\D", "", phone)
        return digits[-10:] if len(digits) >= 10 else digits

    def _inject_todo_responsible(
        self,
        payload: dict[str, Any],
        row: CallResultImportRow,
    ) -> tuple[dict[str, Any] | None, str | None]:
        responsible_id, resp_err = resolve_task_responsible_user(
            row, self.settings, self.portal_id, self.db,
            operator_user_id=self._responsible_user_id,
        )
        if not responsible_id:
            return None, resp_err
        payload["responsibleId"] = responsible_id
        return payload, None

    def _execute_add_task(self, action: BitrixPreparedAction, row: CallResultImportRow) -> bool:
        deal_id = row.matched_deal_id
        log_ctx = {
            "import_id": action.import_id,
            "row_id": row.id,
            "deal_id": deal_id,
            "call_id": row.call_id,
            "action_type": action.operation_type or action.action_type,
            "method": "tasks.task.add",
        }
        if not deal_id:
            action.last_error = "Нет ID сделки — задача не создана"
            action.response_payload = {
                "call_id": row.call_id,
                "deal_id": None,
                "status": "failed",
                "error_message": action.last_error,
            }
            logger.error("bitrix task skipped: %s error=%s", log_ctx, action.last_error)
            return False

        if self.repo.has_succeeded_task_for_call(row.call_id):
            action.execution_status = "skipped"
            action.last_error = "Задача для этого звонка уже создана"
            action.response_payload = {"status": "skipped", "call_id": row.call_id}
            logger.info("bitrix task skipped (dedup): %s", log_ctx)
            return True

        responsible_id, resp_err = resolve_task_responsible_user(
            row, self.settings, self.portal_id, self.db,
            operator_user_id=self._responsible_user_id,
        )
        if not responsible_id:
            action.last_error = resp_err or "Не удалось определить ответственного"
            action.response_payload = {
                "call_id": row.call_id,
                "deal_id": deal_id,
                "status": "failed",
                "error_message": action.last_error,
            }
            logger.error(
                "bitrix task failed: %s responsible_user_id=null error=%s",
                log_ctx,
                action.last_error,
            )
            return False

        creator_id = resolve_task_creator_user(
            self.settings,
            operator_user_id=self._responsible_user_id,
            responsible_user_id=responsible_id,
        )
        payload = dict(action.payload)
        fields = dict(payload.get("fields") or payload)
        fields["RESPONSIBLE_ID"] = responsible_id
        fields["CREATED_BY"] = creator_id
        payload = {"fields": fields}
        action.request_payload = payload

        webhook_masked = mask_webhook(self.settings.bitrix_webhook_url or "")
        logger.info(
            "bitrix task create start import_id=%s row_id=%s deal_id=%s "
            "responsible_user_id=%s creator_user_id=%s action_type=%s method_url=%s/tasks.task.add payload=%s",
            action.import_id,
            row.id,
            deal_id,
            responsible_id,
            creator_id,
            action.operation_type or action.action_type,
            webhook_masked,
            payload,
        )

        res = self.gateway.create_verified_task(
            payload,
            deal_id=int(deal_id),
            responsible_id=responsible_id,
            portal_id=self.portal_id,
        )

        stored = dict(res.response or {})
        stored["call_id"] = row.call_id
        stored["deal_id"] = deal_id
        if stored.get("responsible_user_id") is None:
            stored["responsible_user_id"] = responsible_id
        stored["creator_user_id"] = creator_id
        if not res.success:
            stored["status"] = "failed"
            stored["error_message"] = res.error
            action.response_payload = stored
            action.last_error = res.error
            if res.external_id:
                action.external_id = str(res.external_id)
            logger.error(
                "bitrix task create failed import_id=%s row_id=%s deal_id=%s "
                "responsible_user_id=%s error=%s response=%s",
                action.import_id,
                row.id,
                deal_id,
                responsible_id,
                res.error,
                stored,
            )
            return False

        action.external_id = str(res.external_id)
        action.response_payload = stored
        logger.info(
            "bitrix task create ok import_id=%s row_id=%s deal_id=%s "
            "responsible_user_id=%s bitrix_task_id=%s bitrix_task_url=%s response=%s",
            action.import_id,
            row.id,
            deal_id,
            stored.get("responsible_user_id"),
            stored.get("bitrix_task_id"),
            stored.get("bitrix_task_url"),
            stored,
        )
        return True
