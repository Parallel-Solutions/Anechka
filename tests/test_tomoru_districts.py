from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import CrmEntity, ENTITY_DEAL
from app.repositories.crm_repository import CrmRepository
from app.services.tomoru_districts import TOMORU_DISTRICT_FIELDS
from app.utils.portal import portal_id_from_webhook


def _portal() -> str:
    return portal_id_from_webhook(get_settings().bitrix_webhook_url)


def _deal(
    db,
    deal_id: int,
    *,
    district: str,
    district_field: str,
    region_id: int = 1069,
    category_id: int = 15,
) -> None:
    db.add(
        CrmEntity(
            portal_id=_portal(),
            entity_type_id=ENTITY_DEAL,
            entity_id=deal_id,
            entity_kind='deal',
            title=f'Deal {deal_id}',
            category_id=category_id,
            stage_id='C15:NEW',
            raw_payload={
                'UF_CRM_5ECE25C5D78E0': region_id,
                district_field: district,
            },
            payload_hash=f'district-{deal_id}',
        )
    )
    db.flush()


@pytest.mark.parametrize('district_field', TOMORU_DISTRICT_FIELDS)
def test_repository_filters_every_historical_district_field(db_session, district_field):
    _deal(
        db_session,
        100,
        district='Мамонтовский муниципальный район',
        district_field=district_field,
    )
    _deal(
        db_session,
        101,
        district='Другой муниципальный район',
        district_field=district_field,
    )

    rows = CrmRepository(db_session, _portal()).list_entities_for_export(
        ENTITY_DEAL,
        category_id=15,
        district_names=['Мамонтовский муниципальный район'],
    )

    assert [row.entity_id for row in rows] == [100]


def test_district_options_are_limited_by_region_and_category(client, db_session):
    _deal(
        db_session,
        200,
        district='Мамонтовский муниципальный район',
        district_field=TOMORU_DISTRICT_FIELDS[0],
    )
    _deal(
        db_session,
        201,
        district='  Алейский   муниципальный район ',
        district_field=TOMORU_DISTRICT_FIELDS[1],
    )
    _deal(
        db_session,
        202,
        district='Район другого региона',
        district_field=TOMORU_DISTRICT_FIELDS[2],
        region_id=1200,
    )
    _deal(
        db_session,
        203,
        district='Район другой воронки',
        district_field=TOMORU_DISTRICT_FIELDS[3],
        category_id=99,
    )

    response = client.get(
        '/api/tomoru/districts',
        params=[('category_id', 15), ('region_id', 1069)],
    )

    assert response.status_code == 200
    assert response.json() == [
        'Алейский муниципальный район',
        'Мамонтовский муниципальный район',
    ]

    rows = CrmRepository(db_session, _portal()).list_entities_for_export(
        ENTITY_DEAL,
        category_id=15,
        region_ids=[1069],
        district_names=['Алейский муниципальный район'],
    )
    assert [row.entity_id for row in rows] == [201]
