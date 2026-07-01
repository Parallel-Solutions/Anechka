"""Call result import API routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app.config import Settings, get_call_result_storage_dir, get_call_results_model
from app.dependencies import get_app_settings, get_call_result_classifier_instance, get_session
from app.models import BitrixPreparedAction, CallResultImportRow, CallResultRowAudit, CrmEntity, utcnow
from app.repositories.call_result_repository import CallResultRepository
from app.schemas_call_results import (
    ActionOut,
    ActionPatchRequest,
    AttemptHistoryOut,
    ExecuteRequest,
    HangupRowOut,
    ImportConfigureRequest,
    ImportDetailOut,
    ImportStatusOut,
    ImportSummaryOut,
    MessageResponse,
    RowLlmDebugOut,
    RowOut,
    RowPatchRequest,
)
from app.services.auth_service import resolve_portal_id
from app.services.call_results.classification_prompt import CallResultClassificationPromptBuilder
from app.services.call_results.llm_input_builder import LlmInputBuilder
from app.services.call_results.llm_schema import SCHEMA_VERSION
from app.services.call_results.call_attempt_aggregator import CallAttemptAggregator
from app.services.call_results.export_service import CallResultExportService
from app.services.call_results.format_detector import FormatDetector
from app.services.call_results.job_service import CallResultJobService
from app.services.call_results.matcher import CallResultMatcher
from app.services.call_results.orchestrator import CallResultOrchestrator
from app.services.call_results.payload_builder import BitrixPayloadBuilder
from app.services.call_results.payload_validator import BitrixPayloadValidator

router = APIRouter(tags=["call-results"])


def _portal_repo(db: Session) -> tuple[str, CallResultRepository]:
    settings = get_app_settings(db)
    portal_id = resolve_portal_id(settings)
    return portal_id, CallResultRepository(db, portal_id)


def _crm_context_for_row(row: CallResultImportRow, matcher: CallResultMatcher) -> dict:
    ctx: dict = {
        "contact_name": None,
        "deal_title": None,
        "region": None,
    }
    if row.matched_deal_id:
        if matcher._deals_by_id is None:
            matcher.build_indexes()
        deal = matcher.get_deal(row.matched_deal_id)
        if deal:
            ctx["deal_title"] = deal.title
    return ctx


def _build_row_llm_debug(
    row: CallResultImportRow,
    db: Session,
    settings: Settings,
    portal_id: str,
) -> RowLlmDebugOut:
    prompt_builder = CallResultClassificationPromptBuilder()
    prompt_version = row.llm_prompt_version or prompt_builder.prompt_version
    model = row.llm_model or get_call_results_model(settings)
    matcher = CallResultMatcher(db, portal_id)
    crm_ctx = _crm_context_for_row(row, matcher)
    bundle = LlmInputBuilder(settings.llm_call_results_max_input_chars).build(
        row.normalized_data or {},
        prompt_version=prompt_version,
        schema_version=row.llm_schema_version or SCHEMA_VERSION,
        model=model,
        crm_context=crm_ctx,
    )
    stored_hash = row.llm_input_hash
    rebuilt_hash = bundle.input_hash
    hash_matches = stored_hash == rebuilt_hash if stored_hash else None
    return RowLlmDebugOut(
        system_prompt=prompt_builder.system_prompt(),
        user_payload=bundle.payload,
        user_message=prompt_builder.user_payload(bundle.payload),
        llm_result=row.llm_result,
        llm_status=row.llm_status,
        llm_required=row.llm_required,
        llm_model=row.llm_model or model,
        llm_provider=row.llm_provider,
        llm_prompt_version=row.llm_prompt_version or prompt_version,
        llm_schema_version=row.llm_schema_version or SCHEMA_VERSION,
        llm_input_truncated=row.llm_input_truncated,
        llm_input_hash=stored_hash,
        rebuilt_input_hash=rebuilt_hash,
        input_hash_matches=hash_matches,
        llm_confidence=row.llm_confidence,
        llm_validation_errors=row.llm_validation_errors,
        llm_error_type=row.llm_error_type,
        llm_duration_ms=row.llm_duration_ms,
        llm_token_usage=row.llm_token_usage,
        deterministic_category=row.deterministic_category,
        deterministic_reason=row.deterministic_reason,
    )


def _row_out(row: CallResultImportRow) -> RowOut:
    nd = row.normalized_data or {}
    return RowOut(
        id=row.id,
        source_row_number=row.source_row_number,
        raw_phone=row.raw_phone,
        normalized_phone=row.normalized_phone,
        match_status=row.match_status,
        match_reason=row.match_reason,
        final_category=row.final_category,
        classification_source=row.classification_source,
        classification_reason=row.classification_reason,
        llm_status=row.llm_status,
        llm_confidence=row.llm_confidence,
        llm_required=row.llm_required,
        skip_reason=row.skip_reason,
        extracted_data=row.extracted_data,
        candidate_matches=row.candidate_matches,
        manually_overridden=row.manually_overridden,
        deterministic_category=row.deterministic_category,
        deterministic_reason=row.deterministic_reason,
        llm_category=row.llm_category,
        llm_result=row.llm_result,
        llm_validation_errors=row.llm_validation_errors,
        matched_deal_id=row.matched_deal_id,
        matched_deal_local_id=row.matched_deal_local_id,
        raw_data=row.raw_data,
        normalized_data=nd,
        technical_status=row.technical_status,
        call_result_display=row.call_result_display,
        attempts=row.attempts,
        called_at=row.called_at,
        callback_at=row.callback_at,
        processing_warnings=row.processing_warnings,
        scenario_events=nd.get("scenario_events"),
        merge_conflict_reason=row.merge_conflict_reason,
        business_signals=row.business_signals,
        primary_outcome=row.primary_outcome,
        needs_manual_review=row.needs_manual_review,
        manual_review_reason=row.manual_review_reason,
        execution_status=row.execution_status,
    )


def _is_hangup_without_answers(row: CallResultImportRow) -> bool:
    sig = row.business_signals or {}
    nd = row.normalized_data or {}
    return bool(sig.get("hangup_without_result")) and not nd.get("has_meaningful_content")


def _build_detail(db: Session, imp, portal_id: str) -> ImportDetailOut:
    settings = get_app_settings(db)
    repo = CallResultRepository(db, portal_id)
    rows = repo.list_rows(imp.id)
    actions = repo.list_actions(imp.id)
    matcher = CallResultMatcher(db, portal_id)
    matcher.build_indexes()
    threshold = settings.llm_call_results_confidence_threshold

    by_method: dict[str, list[ActionOut]] = {}
    row_by_id = {r.id: r for r in rows}
    counts = {"comments": 0, "todos": 0, "tasks": 0}
    disabled_actions = 0

    for a in actions:
        row = row_by_id.get(a.import_row_id)
        deal = matcher.get_deal(row.matched_deal_id) if row and row.matched_deal_id else None
        out = ActionOut(
            id=a.id,
            import_row_id=a.import_row_id,
            source_row_number=row.source_row_number if row else None,
            action_group_id=a.action_group_id,
            method=a.method,
            action_type=a.action_type,
            payload=a.payload,
            human_summary=a.human_summary,
            validation_status=a.validation_status,
            validation_errors=a.validation_errors,
            is_enabled=a.is_enabled,
            user_modified=a.user_modified,
            phone=row.raw_phone if row else None,
            deal_title=deal.title if deal else None,
            bitrix_deal_id=row.matched_deal_id if row else None,
            responsible_name=matcher.get_user_name(deal.assigned_by_id) if deal else None,
            final_category=row.final_category if row else None,
        )
        by_method.setdefault(a.method, []).append(out)
        if a.is_enabled:
            if a.method == "crm.timeline.comment.add":
                counts["comments"] += 1
            elif a.method == "crm.activity.todo.add":
                counts["todos"] += 1
            elif a.method == "tasks.task.add":
                counts["tasks"] += 1
        else:
            disabled_actions += 1

    phones = [r.normalized_phone for r in rows if r.normalized_phone]
    unique_phones = len(set(phones))
    repeat_phones = len(phones) - unique_phones
    exact_duplicates = sum(1 for r in rows if r.is_duplicate)
    meaningful = sum(1 for r in rows if (r.normalized_data or {}).get("has_meaningful_content"))

    manual = [
        _row_out(r) for r in rows
        if r.needs_manual_review
        or r.match_status in ("ambiguous", "conflict", "not_found")
        or r.final_category == "unknown"
        or (r.llm_confidence is not None and r.llm_confidence < threshold)
        or r.merge_conflict_reason
    ]
    agg = CallAttemptAggregator()
    history_groups = agg.group_by_phone(rows)
    attempt_history = [
        AttemptHistoryOut(
            normalized_phone=g.normalized_phone,
            attempts=g.attempts,
            latest_outcome=g.latest_outcome,
            latest_no_answer=g.latest_no_answer,
        )
        for g in history_groups.values()
        if len(g.attempts) > 1
    ]

    hangup_rows: list[HangupRowOut] = []
    for r in rows:
        if not _is_hangup_without_answers(r):
            continue
        deal = matcher.get_deal(r.matched_deal_id) if r.matched_deal_id else None
        hangup_rows.append(
            HangupRowOut(
                id=r.id,
                source_row_number=r.source_row_number,
                phone=r.raw_phone or r.normalized_phone,
                deal_id=r.matched_deal_id,
                deal_title=deal.title if deal else None,
                primary_outcome=r.primary_outcome,
                execution_status=r.execution_status,
            )
        )

    summary = ImportSummaryOut(
        total_rows=imp.total_rows or len(rows),
        unique_phones=unique_phones,
        total_attempts=len(rows),
        exact_duplicates=exact_duplicates,
        repeat_phones=max(0, repeat_phones),
        meaningful_content_rows=meaningful,
        matched_rows=imp.matched_rows or sum(1 for r in rows if r.match_status == "matched"),
        review_rows=len(manual),
        skipped_rows=imp.skipped_rows or sum(1 for r in rows if r.skip_reason),
        ambiguous_rows=sum(1 for r in rows if r.match_status == "ambiguous"),
        not_found_rows=sum(1 for r in rows if r.match_status == "not_found"),
        comments=counts["comments"],
        todos=counts["todos"],
        tasks=counts["tasks"],
        disabled_actions=disabled_actions,
        deterministic_classified=imp.deterministic_classified,
        llm_sent=imp.llm_rows_total,
        llm_completed=imp.llm_rows_completed,
        llm_pending=sum(1 for r in rows if r.llm_status == "pending"),
        llm_failed=imp.llm_rows_failed,
        llm_cached=imp.llm_rows_cached,
        llm_not_required=imp.llm_rows_skipped,
        low_confidence=imp.llm_rows_low_confidence,
        manually_overridden=sum(1 for r in rows if r.manually_overridden),
        robot_callback=sum(1 for r in rows if r.final_category == "robot_callback"),
        refusal=sum(1 for r in rows if r.final_category == "refusal"),
        manual_review=len(manual),
        positive=sum(1 for r in rows if r.primary_outcome in ("positive", "mixed") or (r.business_signals or {}).get("positive")),
        alternate_contact=sum(1 for r in rows if (r.business_signals or {}).get("alternate_contact_requested")),
        callback_later=sum(1 for r in rows if (r.business_signals or {}).get("callback_later_requested")),
        no_answer=sum(1 for r in rows if (r.business_signals or {}).get("no_answer")),
        hangup=sum(1 for r in rows if (r.business_signals or {}).get("hangup_without_result")),
        hangup_without_answers=len(hangup_rows),
        prepared_operations=sum(1 for a in actions if a.execution_status == "prepared"),
        executed_operations=sum(1 for a in actions if a.execution_status == "succeeded"),
        execution_errors=sum(1 for a in actions if a.execution_status == "failed"),
        execute_status=imp.execute_status,
    )

    return ImportDetailOut(
        id=imp.id,
        original_filename=imp.original_filename,
        status=imp.status,
        source_format=imp.source_format,
        batch_id=imp.batch_id,
        exported_at=imp.exported_at,
        import_warnings=imp.import_warnings,
        created_at=imp.created_at,
        processed_at=imp.processed_at,
        error_message=imp.error_message,
        duplicate_of_import_id=imp.duplicate_of_import_id,
        summary=summary,
        rows=[_row_out(r) for r in rows],
        actions_by_method=by_method,
        manual_review=manual,
        attempt_history=attempt_history,
        hangup_rows=hangup_rows,
    )


def _build_status(imp) -> ImportStatusOut:
    return ImportStatusOut(
        id=imp.id,
        original_filename=imp.original_filename,
        status=imp.status,
        error_message=imp.error_message,
        source_format=imp.source_format,
        batch_id=imp.batch_id,
        exported_at=imp.exported_at,
        created_at=imp.created_at,
        processed_at=imp.processed_at,
        summary=ImportSummaryOut(
            total_rows=imp.total_rows,
            matched_rows=imp.matched_rows,
            review_rows=imp.review_rows,
            skipped_rows=imp.skipped_rows,
            deterministic_classified=imp.deterministic_classified,
            llm_sent=imp.llm_rows_total,
            llm_completed=imp.llm_rows_completed,
            llm_failed=imp.llm_rows_failed,
            llm_cached=imp.llm_rows_cached,
            llm_not_required=imp.llm_rows_skipped,
            low_confidence=imp.llm_rows_low_confidence,
        ),
    )


def _allowed_extension(filename: str, settings) -> bool:
    ext = Path(filename).suffix.lower()
    allowed = {e.strip().lower() for e in settings.call_result_allowed_extensions.split(",") if e.strip()}
    return ext in allowed


@router.post("/api/call-results/imports")
async def upload_import(
    file: UploadFile = File(...),
    sheet: str | None = Form(None),
    column_mapping: str | None = Form(None),
    force_duplicate: bool = Form(False),
    db: Session = Depends(get_session),
):
    settings = get_app_settings(db)
    portal_id = resolve_portal_id(settings)
    filename = file.filename or "upload.csv"
    if not _allowed_extension(filename, settings):
        raise HTTPException(status_code=400, detail=f"Недопустимое расширение файла. Разрешены: {settings.call_result_allowed_extensions}")

    content = await file.read()
    mapping = None
    if column_mapping:
        try:
            mapping = json.loads(column_mapping)
        except json.JSONDecodeError:
            raise HTTPException(status_code=422, detail="column_mapping: невалидный JSON")

    classifier = get_call_result_classifier_instance(settings)
    orch = CallResultOrchestrator(db, settings, portal_id, classifier)
    imp, dup = orch.save_uploaded_file(content, filename, force_duplicate=force_duplicate)
    if dup:
        return JSONResponse(status_code=409, content=dup)

    assert imp is not None
    db.commit()

    from app.services.call_results.file_parser import CallResultFileParser
    from app.services.call_results.column_mapper import CallResultColumnMapper

    parsed = CallResultFileParser().parse(content, imp.original_filename, sheet)
    if parsed.error:
        raise HTTPException(status_code=400, detail=parsed.error)

    if len(parsed.sheets) > 1 and not sheet:
        nonempty = [s.name for s in parsed.sheets if s.rows]
        if len(nonempty) > 1:
            return {
                "import_id": imp.id,
                "needs_sheet": True,
                "sheets": nonempty,
                "message": "Выберите лист",
            }

    selected = sheet or parsed.selected_sheet
    sheet_obj = CallResultFileParser().get_sheet(parsed, selected or "")
    if sheet_obj:
        fmt = FormatDetector.detect(sheet_obj.headers, imp.original_filename)
        if fmt.is_tomoru:
            CallResultJobService().submit_process(imp.id, sheet_name=sheet, column_mapping=fmt.auto_mapping)
            return MessageResponse(
                message="Формат определён: Tomoru CSV. Обработка начата.",
                import_id=imp.id,
                source_format="tomoru_csv",
            )
        mr = CallResultColumnMapper().map_headers(sheet_obj.headers, mapping)
        if mr.needs_manual and not mapping:
            return {
                "import_id": imp.id,
                "needs_column_mapping": True,
                "detected_columns": sheet_obj.headers,
                "suggested_mapping": mr.mapping,
                "ambiguous": mr.ambiguous,
                "error": mr.error,
            }

    CallResultJobService().submit_process(imp.id, sheet_name=sheet, column_mapping=mapping)
    return MessageResponse(message="Файл загружен. Обработка начата.", import_id=imp.id)


@router.post("/api/call-results/imports/{import_id}/configure")
def configure_import(
    import_id: int,
    body: ImportConfigureRequest,
    db: Session = Depends(get_session),
):
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    if imp.status not in ("uploaded", "failed", "ready"):
        if imp.status == "processing":
            raise HTTPException(status_code=409, detail="Импорт уже обрабатывается")

    if body.selected_sheet:
        imp.selected_sheet = body.selected_sheet
    if body.column_mapping:
        imp.column_mapping = body.column_mapping
    db.commit()

    settings = get_app_settings(db)
    from app.services.call_results.column_mapper import CallResultColumnMapper
    storage = get_call_result_storage_dir(settings) / imp.storage_key
    from app.services.call_results.file_parser import CallResultFileParser
    parser = CallResultFileParser()
    content = storage.read_bytes()
    sheet_name = body.selected_sheet or imp.selected_sheet
    parsed = parser.parse(content, imp.original_filename, sheet_name)
    sheet_name_resolved = sheet_name or imp.selected_sheet or parsed.selected_sheet
    sheet_obj = parser.get_sheet(parsed, sheet_name_resolved or "")
    if sheet_obj is None:
        raise HTTPException(status_code=400, detail="Лист не найден")
    mapping = body.column_mapping or imp.column_mapping
    mr = CallResultColumnMapper().map_headers(sheet_obj.headers, mapping)
    if mr.needs_manual and not mapping:
        return {
            "import_id": imp.id,
            "needs_column_mapping": True,
            "detected_columns": sheet_obj.headers,
            "suggested_mapping": mr.mapping,
            "ambiguous": mr.ambiguous,
            "error": mr.error,
        }

    CallResultJobService().submit_process(
        import_id,
        sheet_name=sheet_name_resolved,
        column_mapping=mapping,
    )
    return MessageResponse(message="Настройка применена. Обработка начата.", import_id=import_id)


@router.get("/api/call-results/imports/{import_id}/status")
def get_import_status(import_id: int, db: Session = Depends(get_session)):
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return _build_status(imp)


@router.get("/api/call-results/imports/{import_id}")
def get_import(import_id: int, db: Session = Depends(get_session)):
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    return _build_detail(db, imp, portal_id)


@router.get("/api/call-results/imports/{import_id}/rows/{row_id}/llm")
def get_row_llm_debug(import_id: int, row_id: int, db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    portal_id, repo = _portal_repo(db)
    row = repo.get_row(import_id, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")
    return _build_row_llm_debug(row, db, settings, portal_id)


@router.patch("/api/call-results/imports/{import_id}/rows/{row_id}")
def patch_row(
    import_id: int,
    row_id: int,
    body: RowPatchRequest,
    db: Session = Depends(get_session),
):
    settings = get_app_settings(db)
    portal_id, repo = _portal_repo(db)
    row = repo.get_row(import_id, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")

    changed_by = "user"

    if body.matched_deal_local_id is not None:
        deal = db.get(CrmEntity, body.matched_deal_local_id)
        if deal is None or deal.portal_id != portal_id:
            raise HTTPException(status_code=400, detail="Сделка не найдена")
        row.matched_deal_local_id = deal.id
        row.matched_deal_id = deal.entity_id
        row.match_status = "matched"
        row.match_reason = "Сделка выбрана вручную"
        db.add(CallResultRowAudit(
            row_id=row.id, changed_by=changed_by, field_name="matched_deal_local_id",
            old_value=row.matched_deal_local_id, new_value=deal.id,
        ))
    elif body.matched_deal_id is not None:
        matcher = CallResultMatcher(db, portal_id)
        matcher.build_indexes()
        deal = matcher.get_deal(body.matched_deal_id)
        if deal is None:
            raise HTTPException(status_code=400, detail="Сделка не найдена в локальной БД")
        row.matched_deal_id = deal.entity_id
        row.matched_deal_local_id = deal.id
        row.match_status = "matched"
        row.match_reason = "Сделка выбрана вручную"

    if body.business_signals is not None:
        row.business_signals = body.business_signals
    else:
        sig = dict(row.business_signals or {})
        for key in (
            "positive", "alternate_contact_requested", "callback_later_requested",
            "no_answer", "deal_not_found", "explicit_refusal", "hangup_without_result",
            "replacement_contact_required", "needs_manual_review",
        ):
            val = getattr(body, key, None)
            if val is not None:
                sig[key] = val
        if body.primary_outcome:
            row.primary_outcome = body.primary_outcome
        if sig:
            row.business_signals = sig

    if body.final_category:
        row.final_category = body.final_category
    if body.comment:
        row.comment = body.comment
    if body.callback_at:
        row.callback_at = body.callback_at

    ext = dict(row.extracted_data or {})
    for field, key in [
        (body.summary, "summary"),
        (body.next_action, "next_action"),
        (body.email, "email"),
        (body.phone_extension, "phone_extension"),
        (body.full_phone, "full_phone"),
        (body.contact_name, "contact_name"),
    ]:
        if field is not None:
            ext[key] = field
    row.extracted_data = ext
    row.manually_overridden = True
    row.manually_overridden_by = changed_by
    row.manually_overridden_at = utcnow()
    row.classification_source = "manual"

    classifier = get_call_result_classifier_instance(settings)
    orch = CallResultOrchestrator(db, settings, portal_id, classifier)
    orch.rebuild_row(import_id, row_id)
    db.commit()
    return _row_out(row)


@router.patch("/api/call-results/imports/{import_id}/actions/{action_id}")
def patch_action(
    import_id: int,
    action_id: int,
    body: ActionPatchRequest,
    db: Session = Depends(get_session),
):
    settings = get_app_settings(db)
    portal_id, repo = _portal_repo(db)
    action = repo.get_action(import_id, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    row = repo.get_row(import_id, action.import_row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")

    if body.is_enabled is not None:
        action.is_enabled = body.is_enabled

    if not body.reset_to_auto:
        builder = BitrixPayloadBuilder()
        matcher = CallResultMatcher(db, portal_id)
        matcher.build_indexes()
        deal = matcher.get_deal(row.matched_deal_id) if row.matched_deal_id else None
        from app.services.call_results.action_planner import PlannedAction

        pa = PlannedAction(
            method=action.method,
            action_type=action.action_type,
            operation_type=action.operation_type or action.action_type,
            payload=action.payload,
            human_summary=action.human_summary or "",
        )
        action.payload = builder.build(
            pa,
            row,
            bitrix_deal_id=row.matched_deal_id or 0,
            assigned_by_id=deal.assigned_by_id if deal else None,
            service_user_id=settings.bitrix_service_user_id,
            comment_override=body.comment_text,
            todo_title=body.todo_title,
            todo_description=body.todo_description,
            deadline=body.deadline,
            settings=settings,
        )
        action.user_modified = True
        modified = list(action.modified_fields or [])
        modified.append("user_edit")
        action.modified_fields = modified

    pv = BitrixPayloadValidator().validate(action.method, action.payload)
    action.validation_status = pv.status
    action.validation_errors = pv.errors or None
    db.commit()
    return {"id": action.id, "validation_status": action.validation_status}


@router.post("/api/call-results/imports/{import_id}/rebuild")
def rebuild_import(import_id: int, db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    classifier = get_call_result_classifier_instance(settings)
    orch = CallResultOrchestrator(db, settings, portal_id, classifier)
    for row in repo.list_rows(import_id):
        orch.rebuild_row(import_id, row.id)
    db.commit()
    return MessageResponse(message="План пересобран", import_id=import_id)


@router.post("/api/call-results/imports/{import_id}/retry-llm")
def retry_llm(import_id: int, db: Session = Depends(get_session)):
    CallResultJobService().submit_process(import_id, retry_llm_only=True)
    return MessageResponse(message="Прогон через ИИ запущен", import_id=import_id)


@router.post("/api/call-results/imports/{import_id}/restart")
def restart_import(import_id: int, db: Session = Depends(get_session)):
    _, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    if imp.status == "processing":
        raise HTTPException(status_code=409, detail="Импорт уже обрабатывается")
    CallResultJobService().submit_process(
        import_id,
        sheet_name=imp.selected_sheet,
        column_mapping=imp.column_mapping,
    )
    return MessageResponse(message="Парсинг запущен заново", import_id=import_id)


@router.get("/api/call-results/imports/{import_id}/export.json")
def export_json(import_id: int, db: Session = Depends(get_session)):
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    data = CallResultExportService(db, portal_id).export_json(imp)
    return JSONResponse(content=data)


@router.get("/api/call-results/imports/{import_id}/export.xlsx")
def export_xlsx(import_id: int, db: Session = Depends(get_session)):
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    content = CallResultExportService(db, portal_id).export_xlsx_bytes(imp)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="review_{import_id}.xlsx"'},
    )


@router.get("/api/call-results/imports/{import_id}/export.csv")
def export_csv(import_id: int, db: Session = Depends(get_session)):
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    content = CallResultExportService(db, portal_id).export_csv_bytes(imp)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="review_{import_id}.csv"'},
    )


@router.delete("/api/call-results/imports/{import_id}")
def delete_import(import_id: int, db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    portal_id, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    try:
        path = get_call_result_storage_dir(settings) / imp.storage_key
        if path.is_file():
            path.unlink()
    except OSError:
        pass
    repo.delete_import(import_id)
    db.commit()
    return MessageResponse(message="Импорт удалён", import_id=import_id)


@router.post("/api/call-results/imports/{import_id}/execute")
def execute_import(import_id: int, body: ExecuteRequest, db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    if not settings.call_results_bitrix_execution_enabled:
        raise HTTPException(status_code=403, detail="Выполнение отключено (CALL_RESULTS_BITRIX_EXECUTION_ENABLED=false)")
    if body.confirmation_token != "EXECUTE":
        raise HTTPException(status_code=400, detail="Требуется confirmation_token=EXECUTE")
    CallResultJobService().submit_execute(
        import_id,
        row_ids=body.row_ids,
        retry_failed_only=body.retry_failed_only,
    )
    return MessageResponse(message="Выполнение запущено в фоне", import_id=import_id)


@router.get("/api/call-results/imports/{import_id}/execute/status")
def execute_status(import_id: int, db: Session = Depends(get_session)):
    _, repo = _portal_repo(db)
    imp = repo.get_import(import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    actions = repo.list_actions(import_id)
    return {
        "execute_status": imp.execute_status,
        "started_at": imp.execute_started_at,
        "completed_at": imp.execute_completed_at,
        "succeeded": sum(1 for a in actions if a.execution_status == "succeeded"),
        "failed": sum(1 for a in actions if a.execution_status == "failed"),
        "skipped": sum(1 for a in actions if a.execution_status == "skipped"),
        "prepared": sum(1 for a in actions if a.execution_status == "prepared"),
    }


@router.get("/api/call-results/retry-queue")
def list_retry_queue(import_id: int | None = None, status: str | None = None, db: Session = Depends(get_session)):
    portal_id, _ = _portal_repo(db)
    from app.services.call_results.retry_queue_gateway import RetryQueueGateway
    gw = RetryQueueGateway(db, portal_id)
    entries = gw.list_entries(import_id=import_id, status=status)
    return gw.export_rows(entries)


@router.get("/api/call-results/retry-queue/export.csv")
def export_retry_queue_csv(import_id: int | None = None, db: Session = Depends(get_session)):
    import csv
    import io
    portal_id, _ = _portal_repo(db)
    from app.services.call_results.retry_queue_gateway import RetryQueueGateway
    gw = RetryQueueGateway(db, portal_id)
    rows = gw.export_rows(gw.list_entries(import_id=import_id))
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8")


@router.get("/api/call-results/contact-search")
def list_contact_search(import_id: int | None = None, db: Session = Depends(get_session)):
    portal_id, _ = _portal_repo(db)
    from app.services.call_results.contact_search_gateway import ContactSearchGateway
    gw = ContactSearchGateway(db, portal_id)
    entries = gw.list_entries(import_id=import_id)
    return [
        {
            "id": e.id,
            "import_id": e.import_id,
            "row_id": e.row_id,
            "deal_id": e.deal_id,
            "source_phone": e.source_phone,
            "status": e.status,
            "found_contact_id": e.found_contact_id,
            "summary": e.summary,
        }
        for e in entries
    ]


@router.post("/api/call-results/contact-search/{entry_id}/confirm")
def confirm_contact_search(entry_id: int, contact_id: int, phone: str | None = None, db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    portal_id, repo = _portal_repo(db)
    from app.services.call_results.contact_search_gateway import ContactSearchGateway
    from app.services.call_results.crm_action_service import CrmActionService
    gw = ContactSearchGateway(db, portal_id)
    entry = gw.confirm_contact(entry_id, contact_id=contact_id, phone=phone, confirmed_by="user")
    if entry is None:
        raise HTTPException(status_code=404, detail="Entry not found")
    if entry.row_id and entry.import_id:
        row = repo.get_row(entry.import_id, entry.row_id)
        imp = repo.get_import(entry.import_id)
        if row and imp:
            svc = CrmActionService(db, settings, portal_id)
            svc.retry_gw.add(
                import_id=imp.id,
                row_id=row.id,
                deal_id=row.matched_deal_id,
                contact_id=contact_id,
                phone_normalized=phone,
                callback_at=None,
                callback_text=None,
                reason="hangup_replacement_contact",
                source_call_id=row.call_id,
                replacement_contact_id=contact_id,
            )
    db.commit()
    return {"id": entry.id, "status": entry.status}


@router.get("/api/call-results/diagnostics")
def call_results_diagnostics(db: Session = Depends(get_session)):
    settings = get_app_settings(db)
    from app.services.call_results.contact_marker_validator import ContactMarkerValidator
    marker = ContactMarkerValidator(settings).validate()
    return {
        "bitrix_webhook_configured": bool(settings.bitrix_webhook_url),
        "execution_enabled": settings.call_results_bitrix_execution_enabled,
        "contact_marker_configured": marker.configured,
        "contact_marker_validated": marker.validated,
        "contact_marker_error": marker.error,
        "contact_marker_warning": marker.warning,
        "retry_queue_available": True,
        "contact_search_provider": settings.contact_search_provider,
    }
