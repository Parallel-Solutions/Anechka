"""Tests for tel_po_reg region export service."""

from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import ExportValidationError
from app.services.tel_po_reg_service import TelPoRegService


def _make_service():
    return TelPoRegService(
        settings=MagicMock(max_export_size=5000),
        cancel_check=lambda: False,
    )


def test_run_region_phones_export_no_region_id():
    service = _make_service()
    with pytest.raises(ExportValidationError, match="региона"):
        service.run_region_phones_export({"region_name": "Test", "limit": 10})


def test_run_region_phones_export_no_deals():
    service = _make_service()
    service.client.get_deals = MagicMock(return_value=[])
    with pytest.raises(ExportValidationError, match="отсутствуют"):
        service.run_region_phones_export(
            {
                "region_name": "Томская область",
                "region_id": 123,
                "limit": 10,
                "category_id": 15,
            }
        )


def test_run_region_phones_export_success(tmp_path):
    service = _make_service()
    service.client.get_deals = MagicMock(
        return_value=[{"ID": "1", "TITLE": "Deal A"}, {"ID": "2", "TITLE": "Deal B"}]
    )

    def fake_collect(deal_id: int):
        if deal_id == 1:
            from app.services.tel_po_reg_service import ContactPhone

            return [ContactPhone(fio="Телефон компании", phone="84951111111")]
        from app.services.tel_po_reg_service import ContactPhone

        return [ContactPhone(fio="Contact Two", phone="89262222222")]

    service._collect_deal_contacts = fake_collect

    with patch("app.services.tel_po_reg_service.get_export_dir", return_value=tmp_path):
        with patch(
            "app.services.tel_po_reg_service.unique_filepath",
            side_effect=lambda d, name: tmp_path / name,
        ):
            result = service.run_region_phones_export(
                {
                    "region_name": "Томская область",
                    "region_id": 99,
                    "limit": 10,
                    "category_id": 15,
                }
            )

    assert result.endswith(".xlsx")
    assert service.stats.deals_total == 2
    assert service.stats.phones_found == 2
