"""Intelligent export API — open contour without login.

Router prefix: /api/intelligent-export. Legacy routers are untouched.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings, get_export_dir
from app.dependencies import (
    get_ie_user,
    get_session,
    get_settings_dep,
)
from app.models import AppUser, ExportJob
from app.models.intelligent_export import MEMORY_KINDS, MEMORY_SCOPES
from app.repositories.intelligent_export_repository import (
    IeAccessDenied,
    IeNotFound,
    IntelligentExportRepository,
    PlanVersionConflict,
    ScopeContext,
)
from app.services.auth_service import resolve_portal_id
from app.services.export_plan.compiler_v2 import CompileError
from app.services.export_plan.models_v2 import ExportPlan2
from app.services.intelligent_export.audit import audit, list_audit
from app.services.intelligent_export.dictionaries import build_dictionary_tools
from app.services.intelligent_export.errors import ie_error
from app.services.intelligent_export.memory_generator import generate_memory
from app.services.intelligent_export.plan_service import prepare_plan, validation_to_dict
from app.services.intelligent_export.planner import BasePlanner, OpenAIPlanner
from app.services.intelligent_export.preview_service import PreviewService
from app.services.intelligent_export.readiness import compute_readiness
from app.services.intelligent_export.scope import build_scope
from app.services.intelligent_export.templates import (
    build_template_plan,
    get_template,
    list_templates,
)
from app.services.intelligent_export.tomoru_prompts import (
    get_tomoru_prompt,
    list_tomoru_prompts,
)
from app.services.intelligent_export.sheet_processor import make_sheet_processor
from app.services.intelligent_export.transform_engine import TransformContext
from app.services.intelligent_export.service import IntelligentExportService
from app.services.intelligent_export.staleness import compute_sync_state
from app.services.job_service import JobService
from app.services.security_service import validate_download_path

router = APIRouter(prefix="/api/intelligent-export", tags=["intelligent-export"])
logger = logging.getLogger(__name__)


# --- helpers ----------------------------------------------------------------


def _repo(db: Session, settings: Settings, user: AppUser) -> IntelligentExportRepository:
    portal_id = resolve_portal_id(settings)
    return IntelligentExportRepository(db, ScopeContext(user=user, portal_id=portal_id))


def get_planner(settings: Settings = Depends(get_settings_dep)) -> BasePlanner:
    """Planner dependency. Overridden in tests with a deterministic fake."""
    try:
        return OpenAIPlanner(settings)
    except Exception as exc:  # noqa: BLE001
        raise ie_error("AI_UNAVAILABLE", "AI планировщик недоступен") from exc


def _map_repo_errors(exc: Exception):
    if isinstance(exc, IeNotFound):
        return ie_error("CONVERSATION_NOT_FOUND", str(exc) or "Не найдено")
    if isinstance(exc, IeAccessDenied):
        return ie_error("ACCESS_DENIED", "Доступ запрещён")
    if isinstance(exc, PlanVersionConflict):
        return ie_error("PLAN_VERSION_CONFLICT", str(exc))
    return ie_error("VALIDATION_ERROR", str(exc))


# --- schemas ----------------------------------------------------------------


class ConversationCreate(BaseModel):
    title: str = ""


class ConversationPatch(BaseModel):
    title: str | None = None
    status: str | None = None


class PlanSaveRequest(BaseModel):
    plan: dict[str, Any]
    expected_version: int | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class MemoryProposal(BaseModel):
    scope: str
    kind: str
    key: str
    content: str | None = None
    value_json: dict[str, Any] | None = None
    priority: int = 100
    source_conversation_id: int | None = None


class MemoryPatch(BaseModel):
    content: str | None = None
    value_json: dict[str, Any] | None = None
    priority: int | None = None
    key: str | None = None


# --- health / readiness -----------------------------------------------------


@router.get("/health")
def health(
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
):
    """Lightweight readiness probe for the UI banner.

    Reports which portal we resolved and whether the connected database
    actually has CRM data to export — so a wrong/empty DB is visible at a
    glance instead of surfacing as confusing export failures.
    """
    portal_id = resolve_portal_id(settings)
    readiness = compute_readiness(db, portal_id, settings)
    return readiness.to_dict()


# --- quick export templates -------------------------------------------------


@router.get("/templates")
def get_templates():
    """Curated, deterministic export templates (no AI involved)."""
    return {"templates": list_templates()}


@router.post("/templates/{template_key}")
def instantiate_template(
    template_key: str,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    """Materialize a quick-export template into a saved, validated plan version.

    Creates a fresh conversation, builds the plan from guaranteed catalog
    fields, validates it against the live catalog/scope, and saves an immutable
    plan version — ready to preview or run in one click.
    """
    portal_id = resolve_portal_id(settings)
    scope = build_scope(user, settings)
    readiness = compute_readiness(db, portal_id, settings)
    if not readiness.has_data:
        raise ie_error(
            "NO_DATA",
            f"В подключённой базе нет данных CRM для портала «{portal_id}». Сначала выполните импорт.",
            extra={"readiness": readiness.to_dict()},
        )

    plan_dict = build_template_plan(template_key, max_rows=scope.max_rows)
    if plan_dict is None:
        raise ie_error("PLAN_NOT_FOUND", f"Шаблон «{template_key}» не найден")

    prepared = prepare_plan(db, portal_id, scope, plan_dict)
    if not prepared.valid or prepared.plan is None:
        raise ie_error(
            "PLAN_INVALID",
            "Шаблон не прошёл проверку по текущему каталогу",
            extra={"issues": validation_to_dict(prepared.validation)["issues"]},
        )

    repo = _repo(db, settings, user)
    template = get_template(template_key)
    conv = repo.create_conversation(title=template.title if template else plan_dict.get("title", "Быстрый экспорт"))
    plan_json = prepared.plan.model_dump(mode="json")
    validation_dict = validation_to_dict(prepared.validation, status="valid")
    version = repo.save_plan_version(
        conv.id,
        plan_json=plan_json,
        validation_result_json=validation_dict,
        catalog_snapshot_hash=prepared.catalog_hash,
    )
    return {
        "conversation_id": conv.id,
        "version": _version_summary(version),
        "plan": plan_json,
        "validation": validation_dict,
    }


# --- Tomoru chat starters ---------------------------------------------------


@router.get("/chat-prompts")
def get_chat_prompts():
    """Curated Tomoru prompts for the «Диалоги» starter list."""
    return {"prompts": list_tomoru_prompts()}


@router.post("/conversations/from-prompt/{prompt_id}")
def create_conversation_from_prompt(
    prompt_id: str,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
    planner: BasePlanner = Depends(get_planner),
):
    """Create a dialog from a Tomoru starter and send the first chat message."""
    prompt = get_tomoru_prompt(prompt_id)
    if prompt is None:
        raise ie_error("PROMPT_NOT_FOUND", f"Промпт «{prompt_id}» не найден")

    repo = _repo(db, settings, user)
    portal_id = resolve_portal_id(settings)
    scope = build_scope(user, settings)
    conv = repo.create_conversation(prompt.title)
    service = IntelligentExportService(db, settings, portal_id)
    try:
        chat_response = service.chat(repo, planner, scope, conv.id, prompt.prompt)
    except (IeNotFound, IeAccessDenied, PlanVersionConflict) as exc:
        raise _map_repo_errors(exc)

    result = {
        "conversation_id": conv.id,
        "title": conv.title,
        "prompt_id": prompt.id,
        **chat_response,
    }
    if prompt.follow_up_prompt:
        result["follow_up_prompt"] = prompt.follow_up_prompt
    return result


# --- conversations ----------------------------------------------------------


@router.post("/conversations")
def create_conversation(
    payload: ConversationCreate,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    conv = repo.create_conversation(payload.title)
    return {"id": conv.id, "title": conv.title, "status": conv.status}


@router.get("/conversations")
def list_conversations(
    include_archived: bool = False,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    convs = repo.list_conversations(include_archived=include_archived)
    return {
        "conversations": [
            {
                "id": c.id,
                "title": c.title,
                "status": c.status,
                "current_plan_version_id": c.current_plan_version_id,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            }
            for c in convs
        ]
    }


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        conv = repo.get_conversation(conversation_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {
        "id": conv.id,
        "title": conv.title,
        "status": conv.status,
        "current_plan_version_id": conv.current_plan_version_id,
    }


@router.patch("/conversations/{conversation_id}")
def patch_conversation(
    conversation_id: int,
    payload: ConversationPatch,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        conv = repo.update_conversation(conversation_id, title=payload.title, status=payload.status)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {"id": conv.id, "title": conv.title, "status": conv.status}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        conv = repo.archive_conversation(conversation_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {"id": conv.id, "status": conv.status}


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        messages = repo.list_messages(conversation_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "metadata": m.metadata_json,
             "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ]
    }


@router.post("/conversations/{conversation_id}/chat")
def chat(
    conversation_id: int,
    payload: ChatRequest,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
    planner: BasePlanner = Depends(get_planner),
):
    repo = _repo(db, settings, user)
    portal_id = resolve_portal_id(settings)
    scope = build_scope(user, settings)
    try:
        repo.get_conversation(conversation_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    service = IntelligentExportService(db, settings, portal_id)
    try:
        return service.chat(repo, planner, scope, conversation_id, payload.message)
    except (IeNotFound, IeAccessDenied, PlanVersionConflict) as exc:
        raise _map_repo_errors(exc)


# --- plans ------------------------------------------------------------------


def _version_summary(version) -> dict:
    vr = version.validation_result_json or {}
    return {
        "id": version.id,
        "conversation_id": version.conversation_id,
        "version_number": version.version_number,
        "plan_hash": version.plan_hash,
        "catalog_snapshot_hash": version.catalog_snapshot_hash,
        "status": vr.get("status"),
        "valid": vr.get("valid"),
        "title": (version.plan_json or {}).get("title"),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }


@router.post("/conversations/{conversation_id}/plan")
def save_plan(
    conversation_id: int,
    payload: PlanSaveRequest,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    portal_id = resolve_portal_id(settings)
    scope = build_scope(user, settings)
    try:
        repo.get_conversation(conversation_id)
    except Exception as exc:
        raise _map_repo_errors(exc)

    prepared = prepare_plan(db, portal_id, scope, payload.plan)
    plan_json = prepared.plan.model_dump(mode="json") if prepared.plan else payload.plan
    status = "valid" if prepared.valid else "invalid"
    validation = validation_to_dict(prepared.validation, status=status)
    try:
        version = repo.save_plan_version(
            conversation_id,
            plan_json=plan_json,
            validation_result_json=validation,
            catalog_snapshot_hash=prepared.catalog_hash,
            expected_current_version_number=payload.expected_version,
        )
    except PlanVersionConflict as exc:
        raise ie_error("PLAN_VERSION_CONFLICT", str(exc))
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {"version": _version_summary(version), "validation": validation}


@router.get("/conversations/{conversation_id}/plans")
def list_plans(
    conversation_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        versions = repo.list_plan_versions(conversation_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {"plans": [_version_summary(v) for v in versions]}


@router.get("/plans/{plan_version_id}")
def get_plan(
    plan_version_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        version = repo.get_plan_version(plan_version_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {
        "version": _version_summary(version),
        "plan": version.plan_json,
        "validation": version.validation_result_json,
    }


@router.post("/plans/{plan_version_id}/activate")
def activate_plan(
    plan_version_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        version = repo.activate_plan_version(plan_version_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {"version": _version_summary(version)}


@router.post("/plans/{plan_version_id}/clone")
def clone_plan(
    plan_version_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        conv, version = repo.clone_plan_to_new_conversation(plan_version_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return {"conversation_id": conv.id, "version": _version_summary(version)}


# --- count / preview --------------------------------------------------------


def _load_validated(repo, db, settings, user, plan_version_id):
    portal_id = resolve_portal_id(settings)
    scope = build_scope(user, settings)
    try:
        version = repo.get_plan_version(plan_version_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    prepared = prepare_plan(db, portal_id, scope, version.plan_json)
    if not prepared.valid:
        raise ie_error(
            "PLAN_INVALID",
            "План не прошёл проверку",
            extra={"issues": validation_to_dict(prepared.validation)["issues"]},
        )
    return version, prepared, scope, portal_id


def _is_query_timeout(exc: DBAPIError) -> bool:
    orig = getattr(exc, "orig", None)
    if orig is not None:
        if type(orig).__name__ == "QueryCanceled":
            return True
        if "timeout" in str(orig).lower():
            return True
    msg = str(exc).lower()
    return "querycanceled" in msg or "statement timeout" in msg or "timeout" in msg


def _build_sheet_processor(db: Session, portal_id: str):
    """Same transforms + validation + error routing the export runner applies,
    so the preview reflects exactly what the exported file will contain."""
    resolve_label, dict_check = build_dictionary_tools(db, portal_id)
    transform_ctx = TransformContext(resolve_dictionary=resolve_label)
    return make_sheet_processor(transform_ctx, dict_check)


def _run_preview_safe(preview: PreviewService, plan: ExportPlan2) -> dict[str, Any]:
    try:
        return preview.preview(plan)
    except CompileError as exc:
        raise ie_error("PLAN_INVALID", str(exc)) from exc
    except StopIteration:
        raise ie_error("PLAN_INVALID", "Датасет из листа не найден в плане") from None
    except DBAPIError as exc:
        logger.exception("Preview query failed")
        if _is_query_timeout(exc):
            raise ie_error(
                "QUERY_TIMEOUT",
                "Запрос превью превысил лимит времени — упростите план или уменьшите объём данных",
            ) from exc
        raise ie_error("PREVIEW_FAILED", "Ошибка выполнения запроса превью") from exc
    except SQLAlchemyError as exc:
        logger.exception("Preview database error")
        raise ie_error("PREVIEW_FAILED", "Ошибка базы данных при превью") from exc
    except (TypeError, ValueError, AttributeError) as exc:
        logger.exception("Preview row resolution failed")
        raise ie_error("PREVIEW_FAILED", f"Ошибка разбора строк превью: {exc}") from exc


@router.post("/plans/{plan_version_id}/count")
def count_plan(
    plan_version_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    _, prepared, scope, portal_id = _load_validated(repo, db, settings, user, plan_version_id)
    preview = PreviewService(db, settings, portal_id, scope, prepared.catalog)
    counts = preview.count_datasets(prepared.plan)
    return {"dataset_counts": counts, "total_count": sum(counts.values())}


@router.post("/plans/{plan_version_id}/preview")
def preview_plan(
    plan_version_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    version, prepared, scope, portal_id = _load_validated(repo, db, settings, user, plan_version_id)
    sync_state = compute_sync_state(db, portal_id, settings)
    preview = PreviewService(
        db, settings, portal_id, scope, prepared.catalog,
        sheet_processor=_build_sheet_processor(db, portal_id),
    )
    result = _run_preview_safe(preview, prepared.plan)
    catalog_changed = version.catalog_snapshot_hash and version.catalog_snapshot_hash != prepared.catalog_hash
    warnings = list(result.get("warnings", []))
    if catalog_changed:
        warnings.append("Каталог полей изменился с момента сохранения версии плана")
    if sync_state.state == "warning":
        warnings.append("Данные импорта устарели")
    return {
        "plan_version_id": plan_version_id,
        "total_count": result["total_count"],
        "sheets": result["sheets"],
        "warnings": warnings,
        "memory_used": (version.validation_result_json or {}).get("memory_used", []),
        "sync_state": sync_state.to_dict(),
    }


# --- runs / export ----------------------------------------------------------


@router.post("/plans/{plan_version_id}/run")
def run_export(
    plan_version_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    version, prepared, scope, portal_id = _load_validated(repo, db, settings, user, plan_version_id)

    sync_state = compute_sync_state(db, portal_id, settings)
    if not sync_state.export_allowed:
        raise ie_error("IMPORT_STALE", "Данные импорта устарели — выгрузка заблокирована", extra={"sync_state": sync_state.to_dict()})

    run = repo.create_run(
        plan_version_id=plan_version_id,
        conversation_id=version.conversation_id,
        status="queued",
    )
    job = JobService().create_job(
        db,
        mode="intelligent_export",
        parameters={
            "portal_id": portal_id,
            "user_id": user.id,
            "plan_version_id": plan_version_id,
            "run_id": run.id,
        },
    )
    repo.update_run(run.id, export_job_id=job.id, status="running")
    audit(db, portal_id, "export_run", user_id=user.id, object_type="run", object_id=run.id, detail={"plan_version_id": plan_version_id})
    return {"run_id": run.id, "job_id": job.id, "status": "running"}


def _run_payload(run, job: ExportJob | None) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "plan_version_id": run.plan_version_id,
        "row_count": run.row_count,
        "error_row_count": run.error_row_count,
        "export_job_id": run.export_job_id,
        "summary": run.result_summary_json,
        "job": None
        if job is None
        else {
            "status": job.status,
            "progress_percent": job.progress_percent,
            "current_step": job.current_step,
            "error_message": job.error_message,
        },
    }


@router.get("/runs")
def list_runs(
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    runs = repo.list_runs()
    job_map = {}
    return {"runs": [_run_payload(r, job_map.get(r.export_job_id)) for r in runs]}


@router.get("/runs/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        run = repo.get_run(run_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    job = db.get(ExportJob, run.export_job_id) if run.export_job_id else None
    return _run_payload(run, job)


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    run_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        run = repo.get_run(run_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    if run.export_job_id:
        JobService().cancel_job(db, run.export_job_id)
    return {"run_id": run.id, "status": "cancel_requested"}


@router.post("/runs/{run_id}/retry")
def retry_run(
    run_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        run = repo.get_run(run_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    return run_export(run.plan_version_id, db=db, settings=settings, user=user)


@router.get("/runs/{run_id}/download")
def download_run(
    run_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        run = repo.get_run(run_id)  # enforces ownership
    except Exception as exc:
        raise _map_repo_errors(exc)
    if run.status != "completed":
        raise ie_error("EXPORT_NOT_READY", "Файл ещё не готов")
    result_file = (run.result_summary_json or {}).get("result_file")
    try:
        path = validate_download_path(get_export_dir(settings), result_file)
    except ValueError as exc:
        raise ie_error("RUN_NOT_FOUND", str(exc))
    audit(db, resolve_portal_id(settings), "download", user_id=user.id, object_type="run", object_id=run_id)
    return FileResponse(path=str(path), filename=path.name)


# --- memory -----------------------------------------------------------------


def _memory_out(entry) -> dict:
    return {
        "id": entry.id,
        "scope": entry.scope,
        "kind": entry.kind,
        "key": entry.key,
        "content": entry.content,
        "value_json": entry.value_json,
        "status": entry.status,
        "priority": entry.priority,
        "source": entry.source,
        "version": entry.version,
        "is_active": entry.is_active,
    }


@router.get("/memory")
def list_memory(
    scope: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    entries = repo.list_memory(scope=scope, kind=kind, status=status)
    return {"memory": [_memory_out(e) for e in entries]}


@router.post("/memory/generate")
def generate_memory_endpoint(
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    """Profile the current portal data and create db-aware *proposed* memory.

    Read-only profiling; nothing is auto-approved. Re-running is idempotent
    (no duplicates; only still-proposed entries are updated).
    """
    repo = _repo(db, settings, user)
    portal_id = resolve_portal_id(settings)
    result = generate_memory(db, repo, portal_id, settings)
    audit(
        db,
        portal_id,
        "memory_generate",
        user_id=user.id,
        object_type="memory",
        detail={
            "created": len(result["created"]),
            "updated": len(result["updated"]),
            "skipped": len(result["skipped"]),
        },
    )
    return {
        "created": [_memory_out(e) for e in result["created"]],
        "updated": [_memory_out(e) for e in result["updated"]],
        "skipped": [_memory_out(e) for e in result["skipped"]],
    }


@router.post("/memory/proposals")
def propose_memory(
    payload: MemoryProposal,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    if payload.scope not in MEMORY_SCOPES:
        raise ie_error("VALIDATION_ERROR", f"Unknown scope: {payload.scope}")
    if payload.kind not in MEMORY_KINDS:
        raise ie_error("VALIDATION_ERROR", f"Unknown kind: {payload.kind}")
    repo = _repo(db, settings, user)
    # project memory requires admin approval; user memory is auto-approved for owner
    if payload.scope == "project":
        status = "approved" if user.role == "admin" else "proposed"
    else:
        status = "approved"
    entry = repo.create_memory(
        scope=payload.scope,
        kind=payload.kind,
        key=payload.key,
        content=payload.content,
        value_json=payload.value_json,
        status=status,
        source="manual",
        priority=payload.priority,
        source_conversation_id=payload.source_conversation_id,
    )
    return {"memory": _memory_out(entry)}


@router.post("/memory/{memory_id}/approve")
def approve_memory(
    memory_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        entry = repo.get_memory(memory_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    if entry.scope == "project" and user.role != "admin":
        raise ie_error("ACCESS_DENIED", "Только администратор утверждает проектную память")
    if entry.scope == "user" and entry.user_id != user.id and user.role != "admin":
        raise ie_error("ACCESS_DENIED", "Можно утверждать только свою память")
    entry = repo.update_memory(memory_id, status="approved", approved_by_user_id=user.id)
    audit(db, resolve_portal_id(settings), "memory_approve", user_id=user.id, object_type="memory", object_id=memory_id, detail={"scope": entry.scope})
    return {"memory": _memory_out(entry)}


@router.post("/memory/{memory_id}/reject")
def reject_memory(
    memory_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        entry = repo.get_memory(memory_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    if entry.scope == "project" and user.role != "admin":
        raise ie_error("ACCESS_DENIED", "Только администратор отклоняет проектную память")
    entry = repo.update_memory(memory_id, status="rejected")
    return {"memory": _memory_out(entry)}


@router.patch("/memory/{memory_id}")
def patch_memory(
    memory_id: int,
    payload: MemoryPatch,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        entry = repo.get_memory(memory_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    if entry.scope == "project" and user.role != "admin":
        raise ie_error("ACCESS_DENIED", "Только администратор меняет проектную память")
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    entry = repo.update_memory(memory_id, **fields)
    return {"memory": _memory_out(entry)}


@router.delete("/memory/{memory_id}")
def delete_memory(
    memory_id: int,
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    repo = _repo(db, settings, user)
    try:
        entry = repo.get_memory(memory_id)
    except Exception as exc:
        raise _map_repo_errors(exc)
    if entry.scope == "project" and user.role != "admin":
        raise ie_error("ACCESS_DENIED", "Только администратор удаляет проектную память")
    entry = repo.soft_delete_memory(memory_id)
    return {"memory": _memory_out(entry)}


# --- audit (admin) ----------------------------------------------------------


@router.get("/audit")
def get_audit(
    db: Session = Depends(get_session),
    settings: Settings = Depends(get_settings_dep),
    user: AppUser = Depends(get_ie_user),
):
    entries = list_audit(db, resolve_portal_id(settings))
    return {
        "audit": [
            {
                "id": e.id,
                "user_id": e.user_id,
                "action": e.action,
                "object_type": e.object_type,
                "object_id": e.object_id,
                "detail": e.detail_json,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
    }
