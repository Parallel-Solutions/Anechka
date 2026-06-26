"""Фоновое выполнение задач выгрузки."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, merge_db_settings
from app.database import SessionLocal
from app.exceptions import AppError, ExportCancelledError
from app.models import ExportJob, utcnow
from app.services.export_service import ExportService, ExportStatistics
from app.services.full_export_service import FullCategoryExportService
from app.services.settings_service import load_settings_from_db
from app.services.tel_po_reg_service import TelPoRegService

logger = logging.getLogger(__name__)

MAX_EVENT_LOG = 100


class JobService:
    _executor: ThreadPoolExecutor | None = None
    _max_workers: int = 2

    @classmethod
    def get_executor(cls, max_workers: int = 2) -> ThreadPoolExecutor:
        if cls._executor is None:
            cls._max_workers = max_workers
            cls._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="export")
        return cls._executor

    @classmethod
    def shutdown(cls) -> None:
        if cls._executor:
            cls._executor.shutdown(wait=False, cancel_futures=False)
            cls._executor = None

    def recover_interrupted_jobs(self, db: Session) -> None:
        interrupted = db.query(ExportJob).filter(ExportJob.status.in_(("running", "queued"))).all()
        for job in interrupted:
            if job.status == "running":
                job.error_message = "Выполнение было прервано перезапуском приложения"
            else:
                job.error_message = "Задача не была выполнена после перезапуска приложения"
            job.status = "failed"
            job.finished_at = utcnow()
            job.current_step = "Ошибка"
            self._append_event(job, job.error_message)
            self._mark_ie_run(db, job, "failed", job.error_message)
        if interrupted:
            db.commit()

    def create_job(self, db: Session, mode: str, parameters: dict[str, Any]) -> ExportJob:
        safe_params = {k: v for k, v in parameters.items() if "webhook" not in k.lower()}
        safe_params["_mode"] = mode
        job = ExportJob(
            mode=mode,
            status="queued",
            parameters_json=json.dumps(safe_params, ensure_ascii=False),
            current_step="В очереди",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        settings = self._get_settings(db)
        self.get_executor(settings.max_workers).submit(self._run_job, job.id)
        return job

    def cancel_job(self, db: Session, job_id: int) -> ExportJob:
        job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if not job:
            raise ValueError("Задача не найдена")
        job.cancel_requested = True
        self._append_event(job, "Запрошена отмена")
        db.commit()
        db.refresh(job)
        return job

    def retry_job(self, db: Session, job_id: int) -> ExportJob:
        old = db.query(ExportJob).filter(ExportJob.id == job_id).first()
        if not old:
            raise ValueError("Задача не найдена")
        params = json.loads(old.parameters_json)
        return self.create_job(db, old.mode, params)

    def _get_settings(self, db: Session) -> Settings:
        return merge_db_settings(load_settings_from_db(db))

    def _run_job(self, job_id: int) -> None:
        db = SessionLocal()
        try:
            job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
            if not job:
                return
            job.status = "running"
            job.started_at = utcnow()
            job.current_step = "Запуск"
            self._append_event(job, "Задача запущена")
            db.commit()

            settings = self._get_settings(db)
            params = json.loads(job.parameters_json)

            def cancel_check() -> bool:
                db.refresh(job)
                return bool(job.cancel_requested)

            def progress(current: int, total: int, step: str, stats: ExportStatistics) -> None:
                job.progress_current = current
                job.progress_total = total
                job.progress_percent = round((current / total * 100) if total else 0, 1)
                job.current_step = step
                job.statistics_json = json.dumps(stats.to_dict())
                db.commit()

            def log_event(message: str) -> None:
                self._append_event(job, message)
                db.commit()

            if job.mode == "region":
                tel_service = TelPoRegService(
                    settings=settings,
                    cancel_check=cancel_check,
                    progress_callback=progress,
                    log_callback=log_event,
                )
                result_path = tel_service.run_region_phones_export(params)
            elif job.mode == "region_lpr":
                from app.services.lpr_service import load_lpr_config
                from app.services.lpr_tomoru_service import LprTomoruService

                lpr_service = LprTomoruService(
                    settings=settings,
                    cancel_check=cancel_check,
                    lpr_config=load_lpr_config(db),
                    progress_callback=progress,
                    log_callback=log_event,
                )
                result_path = lpr_service.run_lpr_tomoru_export(params)
            elif job.mode == "category_full":
                full_service = FullCategoryExportService(
                    settings=settings,
                    cancel_check=cancel_check,
                    progress_callback=progress,
                    log_callback=log_event,
                )
                result_path = full_service.run_category_full_export(params)
            elif job.mode == "intelligent_export":
                from app.services.intelligent_export.job_runner import run_intelligent_export_job

                def ie_progress(current: int, total: int, step: str) -> None:
                    job.progress_current = current
                    job.progress_total = total
                    job.progress_percent = round((current / total * 100) if total else 0, 1)
                    job.current_step = step
                    db.commit()

                result_path = run_intelligent_export_job(
                    db,
                    settings,
                    params,
                    cancel_check=cancel_check,
                    progress=ie_progress,
                    log=log_event,
                )
            else:
                service = ExportService(
                    settings=settings,
                    cancel_check=cancel_check,
                    progress_callback=progress,
                    log_callback=log_event,
                )
                result_path = service.run_stage_export(params)

            job.status = "completed"
            job.result_file = result_path
            job.finished_at = utcnow()
            job.current_step = "Завершено"
            job.progress_percent = 100.0
            self._append_event(job, "Выгрузка завершена успешно")
            db.commit()
        except ExportCancelledError:
            db.rollback()
            job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
            if job:
                job.status = "cancelled"
                job.error_message = ExportCancelledError.user_message
                job.finished_at = utcnow()
                job.current_step = "Отменено"
                self._append_event(job, job.error_message)
                db.commit()
                self._mark_ie_run(db, job, "cancelled", job.error_message)
        except AppError as exc:
            db.rollback()
            job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
            if job:
                job.status = "failed"
                job.error_message = exc.user_message
                job.finished_at = utcnow()
                job.current_step = "Ошибка"
                self._append_event(job, exc.user_message)
                db.commit()
                self._mark_ie_run(db, job, "failed", exc.user_message)
            logger.exception("Job %s failed", job_id)
        except Exception as exc:
            db.rollback()
            message = "Произошла внутренняя ошибка при выгрузке"
            try:
                job = db.query(ExportJob).filter(ExportJob.id == job_id).first()
                if job:
                    job.status = "failed"
                    job.error_message = message
                    job.finished_at = utcnow()
                    job.current_step = "Ошибка"
                    self._append_event(job, str(exc))
                    db.commit()
                    self._mark_ie_run(db, job, "failed", message)
            except Exception:
                db.rollback()
                self._fail_job_with_fresh_session(job_id, message, str(exc))
            logger.exception("Job %s unexpected error", job_id)
        finally:
            db.close()

    def _fail_job_with_fresh_session(self, job_id: int, message: str, detail: str) -> None:
        fallback = SessionLocal()
        try:
            job = fallback.query(ExportJob).filter(ExportJob.id == job_id).first()
            if not job:
                return
            job.status = "failed"
            job.error_message = message
            job.finished_at = utcnow()
            job.current_step = "Ошибка"
            self._append_event(job, detail)
            fallback.commit()
            self._mark_ie_run(fallback, job, "failed", message)
        except Exception:
            logger.exception("Failed to mark job %s as failed via fallback session", job_id)
        finally:
            fallback.close()

    @staticmethod
    def _mark_ie_run(db: Session, job: ExportJob, status: str, message: str) -> None:
        if job.mode != "intelligent_export":
            return
        try:
            params = json.loads(job.parameters_json or "{}")
            from app.services.intelligent_export.job_runner import mark_run_failed

            mark_run_failed(db, params.get("run_id"), status, message)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to update IE run status for job %s", job.id)

    @staticmethod
    def _append_event(job: ExportJob, message: str) -> None:
        events: list[str] = json.loads(job.event_log_json or "[]")
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        events.append(f"[{ts}] {message}")
        job.event_log_json = json.dumps(events[-MAX_EVENT_LOG:], ensure_ascii=False)
