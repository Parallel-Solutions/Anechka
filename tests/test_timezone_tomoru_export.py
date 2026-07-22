from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.models import ENTITY_DEAL, CrmContactPhone, CrmEntity
from app.repositories.contact_repository import ContactRepository
from app.services.auth_service import resolve_portal_id
from app.services.call_results.deal_timezone_resolver import DealTimezoneResolver
from app.services.lpr_service import LprConfig
from app.services.lpr_tomoru_service import LprReportRow, LprTomoruService
from app.services.timezone_tomoru_export import write_timezone_zip


@pytest.fixture
def export_dir():
    directory = Path("test_exports")
    directory.mkdir(exist_ok=True)
    yield directory
    for child in directory.iterdir():
        if child.is_file():
            child.unlink()
    if not any(directory.iterdir()):
        directory.rmdir()


def test_timezone_zip_contains_one_csv_per_timezone(export_dir):
    rows = [
        LprReportRow("79161111111", "", "", "", 1, "", "", "", timezone="Europe/Moscow"),
        LprReportRow("79212222222", "", "", "", 2, "", "", "", timezone="Asia/Tomsk"),
        LprReportRow("79161111111", "", "", "", 3, "", "", "", timezone="Europe/Moscow"),
    ]

    path = write_timezone_zip(rows, export_dir, "stage", local_call_time="10:00")

    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {
            "Asia_Tomsk.csv",
            "Europe_Moscow.csv",
            "manifest.csv",
            "README.txt",
        }
        assert archive.read("Asia_Tomsk.csv").decode("utf-8-sig").splitlines() == [
            "phone_number",
            "79212222222",
        ]
        manifest = archive.read("manifest.csv").decode("utf-8-sig")
        assert "Asia/Tomsk;10:00;1;Asia_Tomsk.csv" in manifest


def test_timezone_resolver_supports_known_region_id(db_session):
    portal_id = resolve_portal_id(get_settings())
    deal = CrmEntity(
        portal_id=portal_id,
        entity_type_id=ENTITY_DEAL,
        entity_id=100,
        entity_kind="deal",
        title="Томск",
        category_id=15,
        stage_id="C15:NEW",
        raw_payload={"UF_CRM_5ECE25C5D78E0": 1091},
        payload_hash="tz-100",
    )
    db_session.add(deal)
    db_session.commit()

    result = DealTimezoneResolver(db_session, portal_id).resolve_for_deal(deal.id)

    assert result.timezone == "Asia/Tomsk"


def test_lpr_service_writes_timezone_zip(db_session, export_dir):
    settings = get_settings()
    settings.export_dir = str(export_dir.resolve())
    portal_id = resolve_portal_id(settings)
    deal = CrmEntity(
        portal_id=portal_id,
        entity_type_id=ENTITY_DEAL,
        entity_id=200,
        entity_kind="deal",
        title="Томская сделка",
        category_id=15,
        stage_id="C15:NEW",
        raw_payload={"UF_CRM_5ECE25C5D78E0": 1091, "closed": "N"},
        payload_hash="tz-200",
    )
    db_session.add(deal)
    contact_repo = ContactRepository(db_session, portal_id)
    contact_repo.upsert_contact(
        700,
        {"full_name": "Иван Иванов", "post": "директор"},
        raw_payload={"ID": 700, "POST": "директор"},
    )
    db_session.add(
        CrmContactPhone(
            portal_id=portal_id,
            contact_id=700,
            value="+7 921 000-11-22",
            value_type="MOBILE",
            is_primary=True,
        )
    )
    contact_repo.upsert_link(700, ENTITY_DEAL, 200, is_primary=True)
    db_session.commit()
    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=LprConfig(keywords=["директор"], fields=["POST"], stopwords=[]),
        db=db_session,
        portal_id=portal_id,
    )

    result_path = service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "group_by_timezone": True,
            "local_call_time": "10:00",
        }
    )

    assert Path(result_path).suffix == ".zip"
    with zipfile.ZipFile(result_path) as archive:
        assert "Asia_Tomsk.csv" in archive.namelist()
        assert "79210001122" in archive.read("Asia_Tomsk.csv").decode("utf-8-sig")
