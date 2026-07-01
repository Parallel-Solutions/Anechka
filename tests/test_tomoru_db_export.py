"""Tests for Tomoru CSV export from local CRM database."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings
from app.models import ENTITY_DEAL
from app.repositories.contact_repository import ContactRepository
from app.services.lpr_service import LprConfig
from app.services.lpr_tomoru_service import LprTomoruService
from app.utils.portal import portal_id_from_webhook


def _portal() -> str:
    return portal_id_from_webhook(get_settings().bitrix_webhook_url)


def _lpr_config() -> LprConfig:
    return LprConfig(keywords=["директор"], fields=["POST"], stopwords=["уволен"])


def _deal_entity(db, deal_id: int, *, category_id: int = 15, stage_id: str = "C15:NEW", region_id: int | None = None):
    from app.models import CrmEntity

    raw_payload = {
        "id": deal_id,
        "CATEGORY_ID": category_id,
        "STAGE_ID": stage_id,
        "closed": "N",
    }
    if region_id is not None:
        raw_payload["UF_CRM_5ECE25C5D78E0"] = region_id

    e = CrmEntity(
        portal_id=_portal(),
        entity_type_id=ENTITY_DEAL,
        entity_id=deal_id,
        entity_kind="deal",
        title=f"Сделка {deal_id}",
        category_id=category_id,
        stage_id=stage_id,
        raw_payload=raw_payload,
        payload_hash=f"h{deal_id}",
    )
    db.add(e)
    db.flush()
    return e


def _contact(db, contact_id: int, *, post: str, full_name: str):
    repo = ContactRepository(db, _portal())
    return repo.upsert_contact(
        contact_id,
        {"full_name": full_name, "post": post},
        raw_payload={"id": contact_id, "POST": post},
    )


def _phone(db, contact_id: int, value: str, value_type: str):
    from app.models import CrmContactPhone

    db.add(
        CrmContactPhone(
            portal_id=_portal(),
            contact_id=contact_id,
            value=value,
            value_type=value_type,
            is_primary=value_type == "MOBILE",
        )
    )
    db.flush()


def _link(db, contact_id: int, deal_id: int):
    ContactRepository(db, _portal()).upsert_link(contact_id, ENTITY_DEAL, deal_id, is_primary=False)


def test_tomoru_db_export_writes_csv(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 1001, category_id=15, stage_id="C15:NEW")
    _contact(db_session, 501, post="Генеральный директор", full_name="Иван Иванов")
    _phone(db_session, 501, "+79991234567", "MOBILE")
    _link(db_session, 501, 1001)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    result_path = service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
        }
    )

    path = Path(result_path)
    assert path.suffix == ".csv"
    content = path.read_text(encoding="utf-8-sig")
    assert content.splitlines()[0] == "phone_number"
    assert "79991234567" in content
    assert service.phones == ["79991234567"]


def test_tomoru_db_export_no_bitrix_calls(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)
    settings.bitrix_webhook_url = ""

    _deal_entity(db_session, 2002, category_id=15)
    _contact(db_session, 602, post="директор", full_name="Пётр Петров")
    _phone(db_session, 602, "89997654321", "WORK")
    _link(db_session, 602, 2002)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    result_path = service.run_lpr_tomoru_export(
        {"entity_type": "deal", "category_id": 15}
    )
    assert Path(result_path).exists()
    assert len(service.phones) == 1


def test_tomoru_db_export_empty_raises(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    with pytest.raises(Exception, match="не найдены"):
        service.run_lpr_tomoru_export(
            {"entity_type": "deal", "category_id": 99}
        )


def test_tomoru_db_export_filters_by_region(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 3001, category_id=15, region_id=1091)
    _deal_entity(db_session, 3002, category_id=15, region_id=1105)
    _contact(db_session, 701, post="директор", full_name="Томск Томсков")
    _contact(db_session, 702, post="директор", full_name="Москва Москвин")
    _phone(db_session, 701, "+79991111111", "MOBILE")
    _phone(db_session, 702, "+79992222222", "MOBILE")
    _link(db_session, 701, 3001)
    _link(db_session, 702, 3002)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    result_path = service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "region_id": 1091,
            "region_name": "Томская область",
        }
    )

    content = Path(result_path).read_text(encoding="utf-8-sig")
    assert "79991111111" in content
    assert "79992222222" not in content
    assert service.phones == ["79991111111"]


def test_tomoru_db_export_filters_by_multiple_regions(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 3101, category_id=15, region_id=1091)
    _deal_entity(db_session, 3102, category_id=15, region_id=1105)
    _deal_entity(db_session, 3103, category_id=15, region_id=1200)
    _contact(db_session, 711, post="директор", full_name="Томск Томсков")
    _contact(db_session, 712, post="директор", full_name="Москва Москвин")
    _contact(db_session, 713, post="директор", full_name="Другой Регион")
    _phone(db_session, 711, "+79991111111", "MOBILE")
    _phone(db_session, 712, "+79992222222", "MOBILE")
    _phone(db_session, 713, "+79993333333", "MOBILE")
    _link(db_session, 711, 3101)
    _link(db_session, 712, 3102)
    _link(db_session, 713, 3103)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    result_path = service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "region_ids": [1091, 1105],
            "region_names": ["Томская область", "Москва"],
        }
    )

    content = Path(result_path).read_text(encoding="utf-8-sig")
    assert "79991111111" in content
    assert "79992222222" in content
    assert "79993333333" not in content
    assert sorted(service.phones) == ["79991111111", "79992222222"]


def test_tomoru_db_export_one_phone_per_deal(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4001, category_id=15)
    _contact(db_session, 401, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 402, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 401, "+79990000001", "MOBILE")
    _phone(db_session, 402, "+79990000002", "MOBILE")
    _link(db_session, 401, 4001)
    _link(db_session, 402, 4001)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export({"entity_type": "deal", "category_id": 15})
    assert len(service.phones) == 1
    assert service.phones[0] == "79990000002"


def test_tomoru_db_export_contact_override(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4002, category_id=15)
    _contact(db_session, 411, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 412, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 411, "+79990000011", "MOBILE")
    _phone(db_session, 412, "+79990000012", "MOBILE")
    _link(db_session, 411, 4002)
    _link(db_session, 412, 4002)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4002: 411},
        }
    )
    assert service.phones == ["79990000011"]


def test_tomoru_db_export_uses_saved_preferences(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4002, category_id=15)
    _contact(db_session, 411, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 412, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 411, "+79990000011", "MOBILE")
    _phone(db_session, 412, "+79990000012", "MOBILE")
    _link(db_session, 411, 4002)
    _link(db_session, 412, 4002)

    from app.services.tomoru_contact_preferences import set_deal

    set_deal(db_session, _portal(), 4002, [411])

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
        }
    )
    assert service.phones == ["79990000011"]


def test_tomoru_db_export_request_override_wins_over_saved(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4002, category_id=15)
    _contact(db_session, 411, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 412, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 411, "+79990000011", "MOBILE")
    _phone(db_session, 412, "+79990000012", "MOBILE")
    _link(db_session, 411, 4002)
    _link(db_session, 412, 4002)

    from app.services.tomoru_contact_preferences import set_deal

    set_deal(db_session, _portal(), 4002, [411])

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4002: 412},
        }
    )
    assert service.phones == ["79990000012"]


def test_tomoru_db_export_override_zero_uses_company_phone(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)
    settings.bitrix_webhook_url = ""

    company_id = 8800
    _deal_entity(db_session, 4003, category_id=15)
    from app.models import CrmEntity, ENTITY_COMPANY

    db_session.add(
        CrmEntity(
            portal_id=_portal(),
            entity_type_id=ENTITY_COMPANY,
            entity_id=company_id,
            entity_kind="company",
            title="ООО Компания",
            payload_hash="hc8800",
            raw_payload={
                "ID": company_id,
                "TITLE": "ООО Компания",
                "PHONE": [{"VALUE": "84951230000", "VALUE_TYPE": "WORK"}],
            },
        )
    )
    deal = db_session.query(CrmEntity).filter_by(entity_id=4003, entity_type_id=ENTITY_DEAL).one()
    deal.raw_payload = dict(deal.raw_payload or {})
    deal.raw_payload["COMPANY_ID"] = company_id
    _contact(db_session, 421, post="Менеджер", full_name="Менеджер")
    _phone(db_session, 421, "+79990000021", "MOBILE")
    _link(db_session, 421, 4003)
    db_session.commit()

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4003: 0},
        }
    )
    assert service.phones == ["74951230000"]


def test_tomoru_db_export_multi_contact_override(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4002, category_id=15)
    _contact(db_session, 411, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 412, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 411, "+79990000011", "MOBILE")
    _phone(db_session, 412, "+79990000012", "MOBILE")
    _link(db_session, 411, 4002)
    _link(db_session, 412, 4002)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4002: [411, 412]},
        }
    )
    assert sorted(service.phones) == ["79990000011", "79990000012"]


def test_tomoru_db_export_contact_and_company_override(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)
    settings.bitrix_webhook_url = ""

    company_id = 8801
    _deal_entity(db_session, 4004, category_id=15)
    from app.models import CrmEntity, ENTITY_COMPANY

    db_session.add(
        CrmEntity(
            portal_id=_portal(),
            entity_type_id=ENTITY_COMPANY,
            entity_id=company_id,
            entity_kind="company",
            title="ООО Компания 2",
            payload_hash="hc8801",
            raw_payload={
                "ID": company_id,
                "TITLE": "ООО Компания 2",
                "PHONE": [{"VALUE": "84951230001", "VALUE_TYPE": "WORK"}],
            },
        )
    )
    deal = db_session.query(CrmEntity).filter_by(entity_id=4004, entity_type_id=ENTITY_DEAL).one()
    deal.raw_payload = dict(deal.raw_payload or {})
    deal.raw_payload["COMPANY_ID"] = company_id
    _contact(db_session, 431, post="Менеджер", full_name="Менеджер")
    _phone(db_session, 431, "+79990000031", "MOBILE")
    _link(db_session, 431, 4004)
    db_session.commit()

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4004: [431, 0]},
        }
    )
    assert sorted(service.phones) == ["74951230001", "79990000031"]


def test_tomoru_db_export_empty_override_skips_deal(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4005, category_id=15)
    _contact(db_session, 441, post="Менеджер", full_name="Менеджер")
    _phone(db_session, 441, "+79990000041", "MOBILE")
    _link(db_session, 441, 4005)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4005: []},
        }
    )
    assert service.phones == []


def test_tomoru_db_export_uses_saved_preferences(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4010, category_id=15)
    _contact(db_session, 5010, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 5011, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 5010, "+79990000051", "MOBILE")
    _phone(db_session, 5011, "+79990000052", "MOBILE")
    _link(db_session, 5010, 4010)
    _link(db_session, 5011, 4010)

    from app.services.tomoru_contact_preferences import set_deal

    set_deal(db_session, _portal(), 4010, [5010])

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
        }
    )
    assert service.phones == ["79990000051"]


def test_tomoru_db_export_request_override_wins_over_saved(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    _deal_entity(db_session, 4011, category_id=15)
    _contact(db_session, 5020, post="Менеджер", full_name="Менеджер")
    _contact(db_session, 5021, post="Генеральный директор", full_name="Директор")
    _phone(db_session, 5020, "+79990000061", "MOBILE")
    _phone(db_session, 5021, "+79990000062", "MOBILE")
    _link(db_session, 5020, 4011)
    _link(db_session, 5021, 4011)

    from app.services.tomoru_contact_preferences import set_deal

    set_deal(db_session, _portal(), 4011, [5020])

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "contact_overrides": {4011: 5021},
        }
    )
    assert service.phones == ["79990000062"]


def test_tomoru_db_export_not_truncated_for_large_selection(db_session, tmp_path):
    settings = get_settings()
    settings.export_dir = str(tmp_path)

    stage_id = "C15:BIG5010"
    for deal_id in range(6000, 11010):
        _deal_entity(db_session, deal_id, category_id=15, stage_id=stage_id)

    service = LprTomoruService(
        settings=settings,
        cancel_check=lambda: False,
        lpr_config=_lpr_config(),
        db=db_session,
        portal_id=_portal(),
    )
    service.run_lpr_tomoru_export(
        {
            "entity_type": "deal",
            "category_id": 15,
            "stage_ids": [stage_id],
        }
    )

    assert service.last_truncated is False
    assert service.last_matched_total == 5010
    assert service.stats.deals_total == 5010
