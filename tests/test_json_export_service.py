"""Tests for JSON export service."""

import json
from dataclasses import dataclass
from pathlib import Path

from app.services.excel_service import ExcelService, NormalizedRow
from app.services.json_export_service import (
    build_export_payload,
    build_json_from_xlsx,
    write_export_json,
)


@dataclass
class SampleContact:
    fio: str
    phone: str


def test_build_export_payload_serializes_dataclasses():
    row = NormalizedRow(
        deal_id=1,
        deal_title="Test",
        category_id=15,
        category_name="Sales",
        stage_id="C15:NEW",
        stage_name="New",
        assigned_id=10,
        assigned_name="Ivan",
        company_id=None,
        company_name="",
        contact_id=20,
        contact_name="Petr",
        raw_phone="+79991234567",
        normalized_phone="79991234567",
        phone_type="WORK",
        phone_source="primary_contact",
        region="Tomsk",
        export_date="2026-06-24 12:00:00 UTC",
    )
    payload = build_export_payload(
        mode="stage",
        export_date="2026-06-24 12:00:00 UTC",
        info={"Сделок": 1},
        data={"format": "normalized", "rows": [row]},
    )

    assert payload["meta"]["mode"] == "stage"
    assert payload["data"]["rows"][0]["deal_id"] == 1
    assert payload["data"]["rows"][0]["contact_name"] == "Petr"


def test_build_export_payload_serializes_nested_contacts():
    payload = build_export_payload(
        mode="region",
        export_date="2026-06-24 12:00:00 UTC",
        info={},
        data={
            "deals": [
                {
                    "deal_id": 5,
                    "deal_title": "Deal",
                    "contacts": [SampleContact(fio="Anna", phone="+7999")],
                }
            ]
        },
    )

    contact = payload["data"]["deals"][0]["contacts"][0]
    assert contact == {"fio": "Anna", "phone": "+7999"}


def test_write_export_json_creates_file_next_to_xlsx(tmp_path: Path):
    xlsx_path = tmp_path / "20260624_stage_test.xlsx"
    xlsx_path.write_text("xlsx", encoding="utf-8")

    json_path = write_export_json(
        xlsx_path,
        build_export_payload(
            mode="stage",
            export_date="2026-06-24 12:00:00 UTC",
            info={"Сделок": 0},
            data={"format": "normalized", "rows": []},
        ),
    )

    assert json_path == xlsx_path.with_suffix(".json")
    assert json_path.exists()
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["meta"]["mode"] == "stage"
    assert parsed["data"]["rows"] == []


def test_build_json_from_xlsx_category_full(tmp_path: Path):
    excel = ExcelService()
    filepath = tmp_path / "full.xlsx"
    deals = [{"ID": "1", "TITLE": "Deal 1", "_stage_name": "New"}]
    contacts = [{"ID": "10", "NAME": "Ivan"}]
    companies = [{"ID": "20", "TITLE": "ACME"}]
    deal_contacts = [{"DEAL_ID": 1, "CONTACT_ID": 10, "IS_PRIMARY": "Y", "SORT": 10}]

    excel.build_full_export(
        deals=deals,
        contacts=contacts,
        companies=companies,
        deal_contacts=deal_contacts,
        deal_field_titles={"ID": "ID", "TITLE": "Title"},
        contact_field_titles={"ID": "ID", "NAME": "Name"},
        company_field_titles={"ID": "ID", "TITLE": "Title"},
        info={"Режим": "category_full", "Дата выгрузки": "2026-06-24 12:00:00 UTC"},
        filepath=filepath,
    )

    payload = build_json_from_xlsx(filepath, "category_full")
    assert payload["meta"]["mode"] == "category_full"
    assert payload["meta"]["source"] == "xlsx_fallback"
    assert payload["data"]["deals"][0]["TITLE"] == "Deal 1"
    assert payload["data"]["contacts"][0]["NAME"] == "Ivan"
