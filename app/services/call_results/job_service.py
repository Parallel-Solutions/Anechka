"""Background job processing for call result imports."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import Settings, merge_db_settings
from app.database import SessionLocal
from app.dependencies import get_call_result_classifier_instance
from app.services.auth_service import resolve_portal_id
from app.services.call_results.orchestrator import CallResultOrchestrator
from app.services.settings_service import load_settings_from_db

logger = logging.getLogger(__name__)


class CallResultJobService:
    _executor: ThreadPoolExecutor | None = None

    @classmethod
    def get_executor(cls, max_workers: int = 2) -> ThreadPoolExecutor:
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="call_result")
        return cls._executor

    @classmethod
    def shutdown(cls) -> None:
        if cls._executor:
            cls._executor.shutdown(wait=False)
            cls._executor = None

    def submit_process(
        self,
        import_id: int,
        *,
        sheet_name: str | None = None,
        column_mapping: dict | None = None,
        retry_llm_only: bool = False,
    ) -> None:
        self.get_executor().submit(
            self._run,
            import_id,
            sheet_name,
            column_mapping,
            retry_llm_only,
        )

    @classmethod
    def submit_execute(
        cls,
        import_id: int,
        *,
        row_ids: list[int] | None = None,
        retry_failed_only: bool = False,
    ) -> None:
        cls.get_executor().submit(cls._run_execute, import_id, row_ids, retry_failed_only)

    @staticmethod
    def _run_execute(
        import_id: int,
        row_ids: list[int] | None,
        retry_failed_only: bool,
    ) -> None:
        db = SessionLocal()
        try:
            settings = merge_db_settings(load_settings_from_db(db))
            portal_id = resolve_portal_id(settings)
            from app.services.call_results.crm_action_service import CrmActionService
            svc = CrmActionService(db, settings, portal_id)
            svc.execute_import(import_id, row_ids=row_ids, retry_failed_only=retry_failed_only)
        except Exception:
            logger.exception("Execute job failed for import %s", import_id)
        finally:
            db.close()

    @staticmethod
    def _run(
        import_id: int,
        sheet_name: str | None,
        column_mapping: dict | None,
        retry_llm_only: bool,
    ) -> None:
        db = SessionLocal()
        try:
            settings = merge_db_settings(load_settings_from_db(db))
            portal_id = resolve_portal_id(settings)
            classifier = get_call_result_classifier_instance(settings)
            orch = CallResultOrchestrator(db, settings, portal_id, classifier)
            orch.process_import(
                import_id,
                sheet_name=sheet_name,
                column_mapping=column_mapping,
                retry_llm_only=retry_llm_only,
            )
        except Exception:
            logger.exception("Call result job failed for import %s", import_id)
        finally:
            db.close()
