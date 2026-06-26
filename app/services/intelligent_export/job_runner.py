"""Bridge between JobService worker and the intelligent export runner."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings, get_export_dir
from app.models import AppUser, IeExportPlanVersion, IeExportRun, utcnow
from app.services.export_plan.catalog import FieldCatalog
from app.services.intelligent_export.plan_service import prepare_plan
from app.services.intelligent_export.runner import IntelligentExportRunner
from app.services.intelligent_export.scope import build_scope
from app.services.intelligent_export.staleness import compute_sync_state
from app.services.security_service import safe_filename, unique_filepath


def run_intelligent_export_job(
    db: Session,
    settings: Settings,
    params: dict[str, Any],
    *,
    cancel_check: Callable[[], bool],
    progress: Callable[[int, int, str], None],
    log: Callable[[str], None],
) -> str:
    portal_id = params["portal_id"]
    plan_version_id = int(params["plan_version_id"])
    user_id = int(params["user_id"])
    run_id = params.get("run_id")

    version = db.get(IeExportPlanVersion, plan_version_id)
    if version is None:
        raise ValueError("Версия плана не найдена")
    user = db.get(AppUser, user_id)
    if user is None:
        raise ValueError("Пользователь не найден")

    scope = build_scope(user, settings)
    prepared = prepare_plan(db, portal_id, scope, version.plan_json)
    if not prepared.valid or prepared.plan is None:
        raise ValueError("План не прошёл проверку и не может быть выгружен")

    sync_state = compute_sync_state(db, portal_id, settings)
    if not sync_state.export_allowed:
        raise ValueError("Данные импорта устарели — полная выгрузка заблокирована")

    plan = prepared.plan
    ext = "csv" if plan.workbook.format == "csv" else "xlsx"
    export_dir = get_export_dir(settings)
    filename = safe_filename("intelligent_export", plan.workbook.filename_label or plan.title, ext=ext)
    dest_path = unique_filepath(export_dir, filename)

    log(f"Запуск выгрузки: {plan.title}")
    runner = IntelligentExportRunner(
        db, settings, portal_id, scope, prepared.catalog,
        cancel_check=cancel_check, progress=progress, log=log,
    )
    result = runner.run(plan, dest_path=dest_path)

    if run_id is not None:
        run = db.get(IeExportRun, int(run_id))
        if run is not None:
            run.status = "completed"
            run.row_count = result.total_rows
            run.error_row_count = sum(s.get("error_rows", 0) for s in result.sheet_summaries.values())
            run.result_summary_json = {
                "sheets": result.sheet_summaries,
                "result_file": str(result.filepath),
                "total_rows": result.total_rows,
            }
            run.finished_at = utcnow()
            db.commit()

    return str(result.filepath)


def mark_run_failed(db: Session, run_id: Any, status: str, message: str) -> None:
    if run_id is None:
        return
    run = db.get(IeExportRun, int(run_id))
    if run is None:
        return
    run.status = status
    run.result_summary_json = {"error": message}
    run.finished_at = utcnow()
    db.commit()
