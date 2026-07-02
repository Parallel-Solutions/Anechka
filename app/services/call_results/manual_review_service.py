"""Resolve manual-review rows via operator actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.config import Settings
from app.models import CallResultImportRow, utcnow
from app.repositories.call_result_repository import CallResultRepository
from app.services.call_results.llm_schema import AlternateContactData, CallResultSignals, compute_primary_outcome
from app.services.call_results.lpr_contact_search_provider import (
    LprContactSearchError,
    LprContactSearchProvider,
)
from app.services.call_results.manual_review_ai_service import ManualReviewAiService
from app.services.call_results.matcher import CallResultMatcher
from app.services.call_results.orchestrator import CallResultOrchestrator
from app.services.call_results.payload_builder import BitrixPayloadBuilder
from app.services.call_results.payload_validator import BitrixPayloadValidator
from app.services.call_results.retry_queue_gateway import RetryQueueGateway
from app.services.call_results.action_planner import PlannedAction

ManualResolveAction = Literal["comment", "todo", "find_contact", "create_contact"]


class ManualReviewError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ManualPreviewResult:
    action: ManualResolveAction
    preview_text: str | None = None
    todo_title: str | None = None
    contact_data: AlternateContactData | None = None
    found_contact: dict[str, Any] | None = None
    search_method: str | None = None
    ai_keywords: list[str] | None = None
    error: str | None = None


@dataclass
class ManualResolveResult:
    action: ManualResolveAction
    message: str
    row_id: int
    prepared_method: str | None = None
    retry_queue_entry_id: int | None = None
    contact_id: int | None = None
    phone: str | None = None
    lpr_reason: str | None = None


@dataclass
class ManualResolveConfirm:
    preview_text: str | None = None
    todo_title: str | None = None
    todo_description: str | None = None
    contact_data: AlternateContactData | None = None
    found_contact_id: int | None = None
    found_phone: str | None = None


class ManualReviewService:
    def __init__(
        self,
        db: Session,
        settings: Settings,
        portal_id: str,
        orchestrator: CallResultOrchestrator,
    ):
        self.db = db
        self.settings = settings
        self.portal_id = portal_id
        self.repo = CallResultRepository(db, portal_id)
        self.matcher = CallResultMatcher(db, portal_id)
        self.orchestrator = orchestrator
        self.retry_gw = RetryQueueGateway(db, portal_id)
        self.lpr_search = LprContactSearchProvider(db, portal_id, settings)
        self.ai = ManualReviewAiService(settings)
        self.payload_builder = BitrixPayloadBuilder()
        self.payload_validator = BitrixPayloadValidator()

    def preview(
        self,
        import_id: int,
        row_id: int,
        action: ManualResolveAction,
    ) -> ManualPreviewResult:
        row = self.repo.get_row(import_id, row_id)
        if row is None:
            raise ManualReviewError("Строка не найдена", status_code=404)

        if action in ("comment", "todo", "find_contact") and not row.matched_deal_id:
            raise ManualReviewError("Сделка не найдена — действие недоступно")

        transcript = self._row_transcript(row)

        if action == "comment":
            return ManualPreviewResult(action="comment", preview_text=transcript or "—")

        if action == "todo":
            return self._preview_todo(row, transcript)

        if action == "create_contact":
            return self._preview_create_contact(row, transcript)

        if action == "find_contact":
            return self._preview_find_contact(row, transcript)

        raise ManualReviewError(f"Неизвестное действие: {action}")

    def resolve(
        self,
        import_id: int,
        row_id: int,
        action: ManualResolveAction,
        confirm: ManualResolveConfirm | None = None,
    ) -> ManualResolveResult:
        row = self.repo.get_row(import_id, row_id)
        if row is None:
            raise ManualReviewError("Строка не найдена", status_code=404)

        if action in ("comment", "todo", "find_contact") and not row.matched_deal_id:
            raise ManualReviewError("Сделка не найдена — действие недоступно")

        confirm = confirm or ManualResolveConfirm()

        if action == "comment":
            return self._resolve_comment(import_id, row, comment_override=confirm.preview_text)
        if action == "todo":
            return self._resolve_todo(
                import_id,
                row,
                todo_title=confirm.todo_title,
                todo_description=confirm.todo_description or confirm.preview_text,
            )
        if action == "find_contact":
            return self._resolve_find_contact(
                import_id,
                row,
                contact_id=confirm.found_contact_id,
                phone=confirm.found_phone,
            )
        if action == "create_contact":
            return self._resolve_create_contact(import_id, row, contact_data=confirm.contact_data)
        raise ManualReviewError(f"Неизвестное действие: {action}")

    def _preview_todo(self, row: CallResultImportRow, transcript: str) -> ManualPreviewResult:
        deal_title = ""
        if self.matcher._deals_by_id is None:
            self.matcher.build_indexes()
        deal = self.matcher.get_deal(row.matched_deal_id)
        if deal:
            deal_title = deal.title or ""

        outcome = self.ai.generate_todo_content(transcript, deal_title=deal_title)
        if outcome.todo:
            return ManualPreviewResult(
                action="todo",
                todo_title=outcome.todo.title,
                preview_text=outcome.todo.description,
            )

        return ManualPreviewResult(
            action="todo",
            todo_title="Обработать положительный результат обзвона",
            preview_text=transcript or self._existing_summary(row) or "—",
        )

    def _preview_create_contact(self, row: CallResultImportRow, transcript: str) -> ManualPreviewResult:
        fallback_phone = row.normalized_phone or row.raw_phone
        outcome = self.ai.extract_contact_data(transcript)
        if outcome.contact:
            contact = outcome.contact
            if not contact.phone and fallback_phone:
                contact = contact.model_copy(update={"phone": fallback_phone})
            return ManualPreviewResult(action="create_contact", contact_data=contact)

        if fallback_phone:
            return ManualPreviewResult(
                action="create_contact",
                contact_data=AlternateContactData(phone=fallback_phone),
            )

        return ManualPreviewResult(
            action="create_contact",
            error="Не удалось извлечь контакт из диалога и нет телефона строки",
        )

    def _preview_find_contact(self, row: CallResultImportRow, transcript: str) -> ManualPreviewResult:
        exclude_phone = row.normalized_phone or row.raw_phone
        exclude_contact_id = row.matched_contact_id
        deal_id = int(row.matched_deal_id)

        ai_keywords: list[str] | None = None
        if self.ai.enabled and transcript.strip():
            outcome = self.ai.extract_search_keywords(transcript)
            if outcome.keywords and outcome.keywords.keywords:
                ai_keywords = outcome.keywords.keywords
                try:
                    found = self.lpr_search.find_by_keywords(
                        deal_id=deal_id,
                        keywords=ai_keywords,
                        exclude_phone=exclude_phone,
                        exclude_contact_id=exclude_contact_id,
                    )
                except LprContactSearchError as exc:
                    raise ManualReviewError(str(exc), status_code=422) from exc
                if found:
                    return ManualPreviewResult(
                        action="find_contact",
                        found_contact=found,
                        search_method="ai_keywords",
                        ai_keywords=ai_keywords,
                    )

        try:
            found = self.lpr_search.find_replacement_contact(
                deal_id=deal_id,
                exclude_phone=exclude_phone,
                exclude_contact_id=exclude_contact_id,
            )
        except LprContactSearchError as exc:
            raise ManualReviewError(str(exc), status_code=422) from exc

        if found is None:
            raise ManualReviewError(
                "Не удалось найти другой контакт",
                status_code=422,
            )

        return ManualPreviewResult(
            action="find_contact",
            found_contact=found,
            search_method="lpr_fallback",
            ai_keywords=ai_keywords,
        )

    @staticmethod
    def _row_transcript(row: CallResultImportRow) -> str:
        nd = row.normalized_data or {}
        events = nd.get("scenario_events") or []
        if events:
            parts: list[str] = []
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                text = (ev.get("transcription") or ev.get("match") or "").strip()
                if not text:
                    continue
                label = (ev.get("field") or "").strip()
                parts.append(f"{label}: {text}" if label else text)
            if parts:
                return "\n\n".join(parts)
        sig = row.business_signals or {}
        ext = row.extracted_data or {}
        return str(sig.get("summary") or ext.get("summary") or row.comment or "").strip()

    def _apply_manual_signals(self, row: CallResultImportRow, signals: CallResultSignals) -> None:
        row.business_signals = signals.to_dict()
        row.primary_outcome = compute_primary_outcome(signals)
        row.needs_manual_review = False
        row.manual_review_reason = None
        row.manually_overridden = True
        row.manually_overridden_by = "user"
        row.manually_overridden_at = utcnow()
        row.classification_source = "manual"
        row.classification_reason = "Ручная проверка оператором"

    def _existing_summary(self, row: CallResultImportRow) -> str:
        sig = row.business_signals or {}
        ext = row.extracted_data or {}
        return str(sig.get("summary") or ext.get("summary") or row.comment or "")

    def _patch_action_payload(
        self,
        import_id: int,
        row: CallResultImportRow,
        method: str,
        *,
        comment_override: str | None = None,
        todo_title: str | None = None,
        todo_description: str | None = None,
    ) -> None:
        actions = [
            a for a in self.repo.list_actions(import_id)
            if a.import_row_id == row.id and a.method == method
        ]
        if not actions:
            return
        if self.matcher._deals_by_id is None:
            self.matcher.build_indexes()
        deal = self.matcher.get_deal(row.matched_deal_id)
        action = actions[0]
        pa = PlannedAction(
            method=action.method,
            action_type=action.action_type,
            operation_type=action.operation_type or action.action_type,
            payload=action.payload,
            human_summary=action.human_summary or "",
        )
        action.payload = self.payload_builder.build(
            pa,
            row,
            bitrix_deal_id=row.matched_deal_id or 0,
            assigned_by_id=deal.assigned_by_id if deal else None,
            service_user_id=self.settings.bitrix_service_user_id,
            comment_override=comment_override,
            todo_title=todo_title,
            todo_description=todo_description,
            deadline=row.callback_at,
            settings=self.settings,
        )
        pv = self.payload_validator.validate(action.method, action.payload)
        action.validation_status = pv.status
        action.validation_errors = pv.errors or None
        action.user_modified = True
        modified = list(action.modified_fields or [])
        modified.append("manual_review_preview")
        action.modified_fields = modified
        self.db.flush()

    def _resolve_comment(
        self,
        import_id: int,
        row: CallResultImportRow,
        *,
        comment_override: str | None = None,
    ) -> ManualResolveResult:
        summary = comment_override or self._existing_summary(row)
        signals = CallResultSignals(
            explicit_refusal=True,
            needs_manual_review=False,
            summary=summary,
            refusal_reason=(row.business_signals or {}).get("refusal_reason"),
        )
        self._apply_manual_signals(row, signals)
        self.orchestrator.rebuild_row(import_id, row.id)
        row = self.repo.get_row(import_id, row.id)
        assert row is not None
        if comment_override:
            self._patch_action_payload(
                import_id,
                row,
                "crm.timeline.comment.add",
                comment_override=comment_override,
            )
        actions = [
            a for a in self.repo.list_actions(import_id)
            if a.import_row_id == row.id and a.method == "crm.timeline.comment.add"
        ]
        if not actions:
            raise ManualReviewError("Не удалось подготовить комментарий для Битрикс24")
        return ManualResolveResult(
            action="comment",
            message="Комментарий подготовлен. Нажмите «Отправить в Bitrix24» для выполнения.",
            row_id=row.id,
            prepared_method="crm.timeline.comment.add",
        )

    def _resolve_todo(
        self,
        import_id: int,
        row: CallResultImportRow,
        *,
        todo_title: str | None = None,
        todo_description: str | None = None,
    ) -> ManualResolveResult:
        if self.matcher._deals_by_id is None:
            self.matcher.build_indexes()
        deal = self.matcher.get_deal(row.matched_deal_id)
        if deal is None:
            raise ManualReviewError("Сделка не найдена — действие недоступно")
        if not deal.assigned_by_id:
            raise ManualReviewError("Нет ответственного по сделке")

        summary = todo_description or self._existing_summary(row)
        signals = CallResultSignals(
            positive=True,
            needs_manual_review=False,
            summary=summary,
        )
        self._apply_manual_signals(row, signals)
        self.orchestrator.rebuild_row(import_id, row.id)
        row = self.repo.get_row(import_id, row.id)
        assert row is not None
        if todo_title or todo_description:
            self._patch_action_payload(
                import_id,
                row,
                "crm.activity.todo.add",
                todo_title=todo_title,
                todo_description=todo_description,
            )
        actions = [
            a for a in self.repo.list_actions(import_id)
            if a.import_row_id == row.id and a.method == "crm.activity.todo.add"
        ]
        if not actions:
            raise ManualReviewError("Не удалось подготовить CRM-дело для Битрикс24")
        return ManualResolveResult(
            action="todo",
            message="CRM-дело подготовлено. Нажмите «Отправить в Bitrix24» для выполнения.",
            row_id=row.id,
            prepared_method="crm.activity.todo.add",
        )

    def _resolve_create_contact(
        self,
        import_id: int,
        row: CallResultImportRow,
        *,
        contact_data: AlternateContactData | None = None,
    ) -> ManualResolveResult:
        phone = (contact_data.phone if contact_data else None) or row.normalized_phone or row.raw_phone
        if not phone:
            raise ManualReviewError("Нет телефона для создания контакта")

        if not self.orchestrator.marker_validator.contact_creation_allowed():
            raise ManualReviewError("Создание контакта отключено — настройте BITRIX_CALL_SOURCE_FIELD_CODE/VALUE")

        summary = self._existing_summary(row)
        signals = CallResultSignals(
            needs_manual_review=False,
            summary=summary,
        )
        if contact_data:
            signals.alternate_contact = contact_data
            ext = dict(row.extracted_data or {})
            if contact_data.name:
                ext["contact_name"] = contact_data.name
            row.extracted_data = ext

        self._apply_manual_signals(row, signals)
        self.db.flush()

        cd_dict = contact_data.model_dump() if contact_data else None
        updated = self.orchestrator.persist_manual_create_contact(
            import_id,
            row.id,
            contact_data=cd_dict,
        )
        if updated is None:
            raise ManualReviewError("Не удалось подготовить создание контакта для Битрикс24")

        actions = [
            a for a in self.repo.list_actions(import_id)
            if a.import_row_id == row.id and a.method == "crm.contact.add"
        ]
        if not actions:
            raise ManualReviewError("Не удалось подготовить создание контакта для Битрикс24")

        return ManualResolveResult(
            action="create_contact",
            message="Создание контакта подготовлено. Нажмите «Отправить в Bitrix24» для выполнения.",
            row_id=row.id,
            prepared_method="crm.contact.add",
        )

    def _resolve_find_contact(
        self,
        import_id: int,
        row: CallResultImportRow,
        *,
        contact_id: int | None = None,
        phone: str | None = None,
    ) -> ManualResolveResult:
        if contact_id and phone:
            found = {
                "contact_id": contact_id,
                "phone": phone,
                "reason": "Подтверждено оператором",
            }
        else:
            try:
                found = self.lpr_search.find_replacement_contact(
                    deal_id=int(row.matched_deal_id),
                    exclude_phone=row.normalized_phone or row.raw_phone,
                    exclude_contact_id=row.matched_contact_id,
                )
            except LprContactSearchError as exc:
                raise ManualReviewError(str(exc), status_code=422) from exc

            if found is None:
                raise ManualReviewError(
                    "Не удалось найти другой контакт по правилам ЛПР",
                    status_code=422,
                )

        entry = self.retry_gw.add(
            import_id=import_id,
            row_id=row.id,
            deal_id=row.matched_deal_id,
            contact_id=found["contact_id"],
            phone_normalized=found["phone"],
            callback_at=None,
            callback_text=None,
            reason="hangup_replacement_contact",
            campaign_id=row.campaign_id,
            source_call_id=row.call_id,
            source_contact_id=row.matched_contact_id,
            replacement_contact_id=found["contact_id"],
            search_required=False,
            status="ready",
        )

        row.needs_manual_review = False
        row.manual_review_reason = None
        row.execution_status = "completed"
        row.manually_overridden = True
        row.manually_overridden_by = "user"
        row.manually_overridden_at = utcnow()
        row.classification_source = "manual"
        row.classification_reason = found.get("reason") or "Найден другой контакт (ЛПР)"
        self.db.flush()

        return ManualResolveResult(
            action="find_contact",
            message="Контакт добавлен в очередь дообзвона.",
            row_id=row.id,
            retry_queue_entry_id=entry.id,
            contact_id=found["contact_id"],
            phone=found.get("phone"),
            lpr_reason=found.get("reason"),
        )
