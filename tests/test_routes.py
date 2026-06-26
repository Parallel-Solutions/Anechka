"""Tests for API routes and security."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models import ExportJob
from app.services.security_service import safe_filename, validate_download_path


def test_safe_filename():
    name = safe_filename("region", "Свердловская область")
    assert name.endswith(".xlsx")
    assert "region" in name
    assert "<" not in name


def test_path_traversal_protection(tmp_path: Path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    safe = export_dir / "ok.xlsx"
    safe.write_text("data")
    result = validate_download_path(export_dir, str(safe))
    assert result.name == "ok.xlsx"

    with pytest.raises(ValueError):
        validate_download_path(export_dir, str(tmp_path / "outside.xlsx"))


def test_home_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ie-app" in resp.text


def test_legacy_export_page(client):
    with patch("app.routers.pages.BitrixClient") as mock_cls:
        mock_cls.return_value.test_connection.return_value = True
        resp = client.get("/legacy-export")
    assert resp.status_code == 200
    assert "Bitrix24" in resp.text


def test_stage_in_category_validation(client):
    with patch("app.routers.exports.BitrixClient") as mock_cls:
        mock_cls.return_value.get_stages.return_value = [{"id": "C15:OTHER", "name": "Other"}]
        resp = client.post(
            "/exports/stage",
            json={
                "category_id": 15,
                "stage_id": "C15:NEW",
                "limit": 10,
                "excluded_user_ids": [],
                "excel_format": "normalized",
            },
        )
    assert resp.status_code == 400
    assert "категории" in resp.json()["detail"]


def test_region_export_creates_job(client):
    with patch("app.routers.exports.job_service") as mock_svc:
        mock_job = MagicMock()
        mock_job.id = 42
        mock_svc.create_job.return_value = mock_job
        resp = client.post(
            "/exports/region",
            json={
                "region_name": "Томская область",
                "region_id": 100,
                "category_id": 15,
                "iblock_id": 49,
                "limit": 500,
            },
        )
    assert resp.status_code == 200
    assert resp.json()["job_id"] == 42
    mock_svc.create_job.assert_called_once()
    args = mock_svc.create_job.call_args[0]
    assert args[1] == "region"
    assert args[2]["region_name"] == "Томская область"
    assert args[2]["limit"] == 500
    assert "excel_format" not in args[2]


def test_category_full_export_creates_job(client):
    with patch("app.routers.exports.job_service") as mock_svc:
        with patch("app.routers.exports.BitrixClient") as mock_cls:
            mock_cls.return_value.get_categories.return_value = [{"id": 15, "name": "Sales"}]
            mock_job = MagicMock()
            mock_job.id = 99
            mock_svc.create_job.return_value = mock_job
            resp = client.post(
                "/exports/category-full",
                json={
                    "category_id": 15,
                    "limit": 5000,
                    "excluded_user_ids": [],
                },
            )
    assert resp.status_code == 200
    assert resp.json()["job_id"] == 99
    mock_svc.create_job.assert_called_once()
    args = mock_svc.create_job.call_args[0]
    assert args[1] == "category_full"
    assert args[2]["category_id"] == 15


def test_category_full_export_limit_validation(client):
    with patch("app.routers.exports.get_app_settings") as mock_settings:
        mock_settings.return_value = MagicMock(max_export_size=5000, bitrix_webhook_url="")
        resp = client.post(
            "/exports/category-full",
            json={"category_id": 15, "limit": 99999},
        )
    assert resp.status_code == 400
    assert "5000" in resp.json()["detail"]


def test_category_full_invalid_category(client):
    with patch("app.routers.exports.BitrixClient") as mock_cls:
        mock_cls.return_value.get_categories.return_value = [{"id": 15, "name": "Sales"}]
        resp = client.post(
            "/exports/category-full",
            json={"category_id": 999, "limit": 100},
        )
    assert resp.status_code == 400
    assert "Категория" in resp.json()["detail"]


def test_cancel_job(client, db_session):
    job = ExportJob(id=1, mode="region", status="running", parameters_json="{}")
    db_session.add(job)
    db_session.commit()

    with patch("app.routers.exports.job_service") as mock_svc:
        job.cancel_requested = True
        mock_svc.cancel_job.return_value = job
        resp = client.post("/api/exports/1/cancel")
    assert resp.status_code == 200


def test_download_blocks_traversal(client, db_session, tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    outside = tmp_path / "secret.xlsx"
    outside.write_text("secret")

    job = ExportJob(
        id=2,
        mode="region",
        status="completed",
        parameters_json="{}",
        result_file=str(outside),
    )
    db_session.add(job)
    db_session.commit()

    with patch("app.routers.exports.get_export_dir", return_value=export_dir):
        resp = client.get("/exports/2/download")
    assert resp.status_code == 403


def test_download_json_success(client, db_session, tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    xlsx_path = export_dir / "export.xlsx"
    xlsx_path.write_text("xlsx", encoding="utf-8")
    json_path = export_dir / "export.json"
    json_path.write_text('{"meta": {"mode": "region"}, "data": {}}', encoding="utf-8")

    job = ExportJob(
        id=3,
        mode="region",
        status="completed",
        parameters_json="{}",
        result_file=str(xlsx_path),
    )
    db_session.add(job)
    db_session.commit()

    with patch("app.routers.exports.get_export_dir", return_value=export_dir):
        resp = client.get("/exports/3/download/json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["meta"]["mode"] == "region"


def test_download_json_missing_file(client, db_session, tmp_path):
    from app.services.excel_service import DealContactsRow, ExcelService

    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    xlsx_path = export_dir / "export.xlsx"
    ExcelService().build_deals_contacts(
        [DealContactsRow(deal_id=1, deal_title="Deal 1", contacts=[("Ivan", "+7999")])],
        xlsx_path,
    )

    job = ExportJob(
        id=4,
        mode="region",
        status="completed",
        parameters_json="{}",
        result_file=str(xlsx_path),
    )
    db_session.add(job)
    db_session.commit()

    with patch("app.routers.exports.get_export_dir", return_value=export_dir):
        resp = client.get("/exports/4/download/json")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["meta"]["mode"] == "region"
    assert payload["meta"]["source"] == "xlsx_fallback"
    assert payload["data"]["deals"][0]["deal_id"] == "1"
    assert (export_dir / "export.json").exists()


def test_download_json_blocks_traversal(client, db_session, tmp_path):
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    outside_xlsx = tmp_path / "outside.xlsx"
    outside_xlsx.write_text("xlsx", encoding="utf-8")
    outside_json = tmp_path / "outside.json"
    outside_json.write_text("{}", encoding="utf-8")

    job = ExportJob(
        id=5,
        mode="region",
        status="completed",
        parameters_json="{}",
        result_file=str(outside_xlsx),
    )
    db_session.add(job)
    db_session.commit()

    with patch("app.routers.exports.get_export_dir", return_value=export_dir):
        resp = client.get("/exports/5/download/json")
    assert resp.status_code == 403
