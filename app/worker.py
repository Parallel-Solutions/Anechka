"""Background worker for Bitrix CRM import jobs."""

from __future__ import annotations

import logging
import signal
import sys
import time

from app.config import get_settings, merge_db_settings
from app.database import SessionLocal
from app.repositories.sync_repository import SyncRepository
from app.services.bitrix_import.orchestrator import ImportOrchestrator
from app.services.bitrix_import.scheduler_service import BitrixImportScheduler
from app.services.settings_service import load_settings_from_db
from app.utils.portal import portal_id_from_webhook

logger = logging.getLogger(__name__)

_REMATCH_AFTER_IMPORT_MODES = frozenset({"incremental", "full", "contacts_backfill"})


def _rematch_call_results_not_found(db, settings, portal_id: str) -> int:
    from app.dependencies import get_call_result_classifier_instance
    from app.services.call_results.matcher import invalidate_matcher_cache
    from app.services.call_results.orchestrator import CallResultOrchestrator

    invalidate_matcher_cache(portal_id)
    classifier = get_call_result_classifier_instance(settings)
    orch = CallResultOrchestrator(db, settings, portal_id, classifier)
    return orch.rematch_all_imports(statuses=("not_found",))

_shutdown = False
_scheduler = BitrixImportScheduler()


def _handle_signal(signum, frame):
    global _shutdown
    logger.info("Shutdown signal received (%s)", signum)
    _shutdown = True


def run_worker() -> None:
    settings = get_settings()
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("Bitrix import worker started")
    while not _shutdown:
        db = SessionLocal()
        try:
            db_settings = merge_db_settings(load_settings_from_db(db))
            sync_repo = SyncRepository(db)
            recovered = sync_repo.recover_stale_runs(settings.worker_stale_run_minutes)
            if recovered:
                logger.info("Recovered %s stale runs", recovered)

            if db_settings.bitrix_webhook_url:
                portal_id = portal_id_from_webhook(db_settings.bitrix_webhook_url)
                _scheduler.maybe_enqueue(db, db_settings, portal_id)

            run = sync_repo.claim_next_run()
            if not run:
                time.sleep(settings.worker_poll_interval)
                continue

            if not db_settings.bitrix_webhook_url:
                sync_repo.complete_run(run.id, "failed", "URL вебхука не настроен")
                continue

            portal_id = portal_id_from_webhook(db_settings.bitrix_webhook_url)
            last_heartbeat = time.monotonic()

            def cancel_check() -> bool:
                db.refresh(run)
                return bool(run.cancel_requested)

            orchestrator = ImportOrchestrator(
                db=db,
                settings=db_settings,
                portal_id=portal_id,
                sync_run_id=run.id,
                cancel_check=cancel_check,
            )

            try:
                analyze = True
                if run.statistics_json and isinstance(run.statistics_json, dict):
                    analyze = run.statistics_json.get("analyze_metadata", True)
                orchestrator.run(run.mode, analyze_metadata=analyze)
                if cancel_check():
                    sync_repo.complete_run(run.id, "cancelled")
                else:
                    stats = orchestrator.stats
                    stats["diagnostics"] = orchestrator.get_diagnostics()
                    sync_repo.update_progress(
                        run.id,
                        statistics_json=stats,
                        api_requests_count=orchestrator.client.api_requests_count,
                        ai_requests_count=orchestrator.ai_service.ai_requests_count,
                    )
                    sync_repo.complete_run(run.id, "completed")
                    if run.mode in _REMATCH_AFTER_IMPORT_MODES:
                        try:
                            rematched = _rematch_call_results_not_found(db, db_settings, portal_id)
                            if rematched:
                                logger.info(
                                    "Call results rematch after %s import: %s not_found rows updated",
                                    run.mode,
                                    rematched,
                                )
                        except Exception:
                            logger.exception("Call results rematch after Bitrix import failed")
                logger.info("Run %s completed", run.id)
            except Exception as exc:
                logger.exception("Run %s failed", run.id)
                sync_repo.complete_run(run.id, "failed", str(exc))
        except Exception:
            logger.exception("Worker loop error")
        finally:
            db.close()

    logger.info("Worker stopped")


if __name__ == "__main__":
    from app.logging_config import setup_logging

    setup_logging(get_settings().log_level)
    run_worker()
