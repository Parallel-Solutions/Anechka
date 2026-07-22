from __future__ import annotations

import io

from openpyxl import Workbook

from app.services.external_phone_import import build_tomoru_csv, extract_external_phones


def test_extract_external_phones_from_semicolon_csv_normalizes_and_deduplicates():
    content = (
        "Имя;Телефон\n"
        "Иван;+7 (916) 123-45-67\n"
        "Повтор;8 916 123-45-67\n"
        "Несколько;4951234567 / +7 921 555-44-33\n"
        "Пусто;нет номера\n"
    ).encode("utf-8-sig")

    result = extract_external_phones(content, "phones.csv")

    assert result.phones == ["79161234567", "74951234567", "79215554433"]
    assert result.rows_total == 4
    assert result.rows_with_phone == 3
    assert result.invalid_rows == 1


def test_extract_external_phones_from_xlsx_phone_alias():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Компания", "Номер телефона"])
    sheet.append(["Ромашка", "8 999 111-22-33"])
    buffer = io.BytesIO()
    workbook.save(buffer)

    result = extract_external_phones(buffer.getvalue(), "phones.xlsx")

    assert result.phones == ["79991112233"]
    assert result.rows_total == 1


def test_build_tomoru_csv_has_expected_single_column():
    content = build_tomoru_csv(["79161234567", "79991112233"]).decode("utf-8-sig")

    assert content.splitlines() == ["phone_number", "79161234567", "79991112233"]


def test_external_phone_page_has_safe_preview_flow(client):
    response = client.get("/tomoru-export")

    assert response.status_code == 200
    assert 'id="external-phone-file"' in response.text
    assert 'id="external-phone-preview"' in response.text
    assert 'id="external-phone-download" disabled' in response.text
    assert "Проверить файл" in response.text

def test_external_phone_download_endpoint(client):
    response = client.post(
        "/exports/tomoru/external/download",
        files={"file": ("phones.csv", "Телефон\n+7 916 123-45-67\n", "text/csv")},
    )

    assert response.status_code == 200
    assert response.headers["x-export-phones"] == "1"
    assert response.headers["x-import-invalid-rows"] == "0"
    assert response.content.decode("utf-8-sig").splitlines() == ["phone_number", "79161234567"]


def test_external_phone_preview_endpoint(client):
    response = client.post(
        "/exports/tomoru/external/preview",
        files={
            "file": (
                "phones.csv",
                """Имя;Телефон
Иван;+7 916 123-45-67
Пусто;нет номера
""",
                "text/csv",
            )
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data == {
        "filename": "phones.csv",
        "rows_total": 2,
        "rows_with_phone": 1,
        "invalid_rows": 1,
        "phones_total": 1,
        "phones": ["79161234567"],
        "preview_truncated": False,
    }


def test_external_phone_preview_is_capped_but_keeps_total(client):
    rows = ["Телефон"] + [str(79000000000 + index) for index in range(105)]
    response = client.post(
        "/exports/tomoru/external/preview",
        files={"file": ("phones.csv", chr(10).join(rows), "text/csv")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["phones_total"] == 105
    assert len(data["phones"]) == 100
    assert data["preview_truncated"] is True

def test_external_phone_download_rejects_unknown_format(client):
    response = client.post(
        "/exports/tomoru/external/download",
        files={"file": ("phones.txt", "79161234567", "text/plain")},
    )

    assert response.status_code == 400
    assert "CSV" in response.json()["detail"]
