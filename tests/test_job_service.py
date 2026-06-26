"""Tests for background export job routing."""

from unittest.mock import MagicMock, patch

from app.models import ExportJob
from app.services.job_service import JobService


def _run_job_with_test_db(service: JobService, job_id: int, db_session) -> None:
    with patch("app.services.job_service.SessionLocal", return_value=db_session):
        with patch.object(db_session, "close"):
            service._run_job(job_id)


def test_run_job_region_uses_tel_po_reg(db_session):
    job = ExportJob(
        id=1,
        mode="region",
        status="queued",
        parameters_json='{"region_name": "Томская область", "region_id": 1091, "limit": 10}',
    )
    db_session.add(job)
    db_session.commit()

    service = JobService()

    with patch.object(service, "_get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(max_workers=1, max_export_size=5000)
        with patch(
            "app.services.tel_po_reg_service.TelPoRegService.run_region_phones_export",
            return_value="/exports/test.xlsx",
        ) as mock_region:
            with patch(
                "app.services.export_service.ExportService.run_stage_export",
            ) as mock_stage:
                _run_job_with_test_db(service, job.id, db_session)

    mock_region.assert_called_once()
    mock_stage.assert_not_called()

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.result_file == "/exports/test.xlsx"


def test_run_job_stage_uses_export_service(db_session):
    job = ExportJob(
        id=2,
        mode="stage",
        status="queued",
        parameters_json='{"category_id": 15, "stage_id": "C15:NEW", "limit": 10}',
    )
    db_session.add(job)
    db_session.commit()

    service = JobService()

    with patch.object(service, "_get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(max_workers=1, max_export_size=5000)
        with patch(
            "app.services.tel_po_reg_service.TelPoRegService.run_region_phones_export",
        ) as mock_region:
            with patch(
                "app.services.export_service.ExportService.run_stage_export",
                return_value="/exports/stage.xlsx",
            ) as mock_stage:
                _run_job_with_test_db(service, job.id, db_session)

    mock_stage.assert_called_once()
    mock_region.assert_not_called()

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.result_file == "/exports/stage.xlsx"


def test_run_job_category_full_uses_full_export_service(db_session):
    job = ExportJob(
        id=3,
        mode="category_full",
        status="queued",
        parameters_json='{"category_id": 15, "limit": 100}',
    )
    db_session.add(job)
    db_session.commit()

    service = JobService()

    with patch.object(service, "_get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(max_workers=1, max_export_size=5000)
        with patch(
            "app.services.tel_po_reg_service.TelPoRegService.run_region_phones_export",
        ) as mock_region:
            with patch(
                "app.services.export_service.ExportService.run_stage_export",
            ) as mock_stage:
                with patch(
                    "app.services.full_export_service.FullCategoryExportService.run_category_full_export",
                    return_value="/exports/full.xlsx",
                ) as mock_full:
                    _run_job_with_test_db(service, job.id, db_session)

    mock_full.assert_called_once()
    mock_region.assert_not_called()
    mock_stage.assert_not_called()

    db_session.refresh(job)
    assert job.status == "completed"
    assert job.result_file == "/exports/full.xlsx"


def test_run_job_intelligent_export_marks_failed_on_compiler_error(db_session):
    job = ExportJob(
        id=10,
        mode="intelligent_export",
        status="queued",
        parameters_json='{"portal_id": "p", "user_id": 1, "plan_version_id": 4, "run_id": 1}',
    )
    db_session.add(job)
    db_session.commit()

    service = JobService()

    with patch.object(service, "_get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(max_workers=1, max_export_size=5000)
        with patch(
            "app.services.intelligent_export.job_runner.run_intelligent_export_job",
            side_effect=RuntimeError("compiler failed"),
        ):
            _run_job_with_test_db(service, job.id, db_session)

    db_session.refresh(job)
    assert job.status == "failed"
    assert job.error_message == "Произошла внутренняя ошибка при выгрузке"
    assert job.finished_at is not None


def test_recover_interrupted_jobs_marks_queued_and_running_failed(db_session):
    queued = ExportJob(
        id=20,
        mode="stage",
        status="queued",
        parameters_json="{}",
        current_step="В очереди",
    )
    running = ExportJob(
        id=21,
        mode="intelligent_export",
        status="running",
        parameters_json='{"run_id": 5, "_mode": "intelligent_export"}',
        current_step="Запуск",
    )
    db_session.add_all([queued, running])
    db_session.commit()

    service = JobService()
    with patch.object(service, "_mark_ie_run") as mark_ie:
        service.recover_interrupted_jobs(db_session)

    db_session.refresh(queued)
    db_session.refresh(running)
    assert queued.status == "failed"
    assert "не была выполнена" in (queued.error_message or "")
    assert running.status == "failed"
    assert "прервано перезапуском" in (running.error_message or "")
    assert mark_ie.call_count == 2
