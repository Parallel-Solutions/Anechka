"""Phase D: intelligent export API integration tests."""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.models import CrmEntity, CrmFieldDefinition, ENTITY_DEAL, SyncCheckpoint

API = "/api/intelligent-export"


def _seed_deal(db, entity_id, category_id=1):
    db.add(
        CrmEntity(
            portal_id=get_settings_portal(db),
            entity_type_id=ENTITY_DEAL,
            entity_id=entity_id,
            title=f"Deal {entity_id}",
            category_id=category_id,
            payload_hash=f"h{entity_id}",
            raw_payload={"id": entity_id, "TITLE": f"Deal {entity_id}"},
        )
    )


def get_settings_portal(db):
    from app.services.auth_service import resolve_portal_id

    return resolve_portal_id(get_settings())


def _seed_checkpoint(db):
    db.add(
        SyncCheckpoint(
            portal_id=get_settings_portal(db),
            resource_name="deal",
            entity_type_id=ENTITY_DEAL,
            last_successful_sync_at=datetime.now(timezone.utc),
        )
    )


def _plan(category=1):
    return {
        "schema_version": "2.0",
        "title": "Сделки",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": 2,
                "sources": [{"alias": "deal", "entity_type_id": 2}],
                "filters": [
                    {"field": {"entity_type_id": 2, "field_code": "CATEGORY_ID", "source_alias": "deal"}, "op": "eq", "value": category}
                ],
                "limit": 100,
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [
                {
                    "id": "s",
                    "name": "Сделки",
                    "mode": "rows",
                    "dataset_id": "deals",
                    "columns": [
                        {"id": "idc", "header": "ID", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"}}},
                        {"id": "title", "header": "Назв", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"}}},
                    ],
                }
            ],
        },
    }


def test_anonymous_access_ok(client):
    assert client.get(f"{API}/conversations").status_code == 200
    assert client.post(f"{API}/conversations", json={}).status_code == 200


def test_conversation_crud_without_login(client, db_session):
    created = client.post(f"{API}/conversations", json={"title": "Plan A"}).json()
    cid = created["id"]
    assert client.get(f"{API}/conversations/{cid}").status_code == 200
    listing = client.get(f"{API}/conversations").json()["conversations"]
    assert any(c["id"] == cid for c in listing)


def test_save_plan_count_preview(client, db_session):
    _seed_deal(db_session, 1, 1)
    _seed_deal(db_session, 2, 1)
    _seed_deal(db_session, 3, 9)
    _seed_checkpoint(db_session)
    db_session.commit()

    cid = client.post(f"{API}/conversations", json={"title": "P"}).json()["id"]
    saved = client.post(f"{API}/conversations/{cid}/plan", json={"plan": _plan(1)})
    assert saved.status_code == 200, saved.text
    assert saved.json()["validation"]["valid"] is True
    version_id = saved.json()["version"]["id"]

    count = client.post(f"{API}/plans/{version_id}/count").json()
    assert count["total_count"] == 2

    preview = client.post(f"{API}/plans/{version_id}/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total_count"] == 2
    assert len(body["sheets"]) == 1
    assert len(body["sheets"][0]["rows"]) == 2
    assert body["sync_state"]["state"] == "normal"


def test_optimistic_conflict_on_save(client, db_session):
    db_session.commit()
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    client.post(f"{API}/conversations/{cid}/plan", json={"plan": _plan()})
    conflict = client.post(f"{API}/conversations/{cid}/plan", json={"plan": _plan(), "expected_version": 0})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "PLAN_VERSION_CONFLICT"


def test_preview_rejects_invalid_plan(client, db_session):
    db_session.commit()
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    bad = _plan()
    bad["workbook"]["sheets"][0]["columns"].append(
        {"id": "ghost", "header": "G", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "NO_SUCH", "source_alias": "deal"}}}
    )
    version_id = client.post(f"{API}/conversations/{cid}/plan", json={"plan": bad}).json()["version"]["id"]
    resp = client.post(f"{API}/plans/{version_id}/preview")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PLAN_INVALID"


def test_memory_approval_workflow(client, db_session):
    db_session.commit()

    proj = client.post(
        f"{API}/memory/proposals",
        json={"scope": "project", "kind": "term", "key": "регион", "content": "город клиента"},
    ).json()["memory"]
    assert proj["status"] == "approved"

    umem = client.post(
        f"{API}/memory/proposals",
        json={"scope": "user", "kind": "preference", "key": "date_fmt", "content": "ДД.ММ.ГГГГ"},
    ).json()["memory"]
    assert umem["status"] == "approved"


def test_preview_returns_422_not_500_on_resolve_error(client, db_session, monkeypatch):
    _seed_deal(db_session, 1, 1)
    _seed_checkpoint(db_session)
    db_session.commit()

    cid = client.post(f"{API}/conversations", json={"title": "P"}).json()["id"]
    version_id = client.post(f"{API}/conversations/{cid}/plan", json={"plan": _plan(1)}).json()["version"]["id"]

    def _boom(*_args, **_kwargs):
        raise TypeError("bad cell")

    monkeypatch.setattr("app.services.intelligent_export.preview_service.resolve_row", _boom)

    resp = client.post(f"{API}/plans/{version_id}/preview")
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PREVIEW_FAILED"


def test_preview_serializes_datetime_columns(client, db_session):
    updated = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
    db_session.add(
        CrmEntity(
            portal_id=get_settings_portal(db_session),
            entity_type_id=ENTITY_DEAL,
            entity_id=1,
            title="Deal 1",
            category_id=1,
            updated_time=updated,
            payload_hash="h1",
            raw_payload={"id": 1, "TITLE": "Deal 1"},
        )
    )
    _seed_checkpoint(db_session)
    db_session.commit()

    plan = _plan(1)
    plan["workbook"]["sheets"][0]["columns"].append(
        {
            "id": "mod",
            "header": "Изменён",
            "value": {
                "kind": "field",
                "field": {"entity_type_id": 2, "field_code": "DATE_MODIFY", "source_alias": "deal"},
            },
        }
    )

    cid = client.post(f"{API}/conversations", json={"title": "Dates"}).json()["id"]
    version_id = client.post(f"{API}/conversations/{cid}/plan", json={"plan": plan}).json()["version"]["id"]

    preview = client.post(f"{API}/plans/{version_id}/preview")
    assert preview.status_code == 200, preview.text
    mod_value = preview.json()["sheets"][0]["rows"][0]["mod"]
    assert mod_value.startswith("2024-06-01T12:00:00")


def test_preview_with_placeholder_region_filter(client, db_session):
    portal_id = get_settings_portal(db_session)
    db_session.add(
        CrmFieldDefinition(
            portal_id=portal_id,
            entity_type_id=ENTITY_DEAL,
            original_field_name="UF_CRM_5ECE25C5D78E0",
            upper_name="UF_CRM_5ECE25C5D78E0",
            field_type="iblock_element",
            is_active=True,
        )
    )
    _seed_deal(db_session, 1, category_id=15)
    _seed_checkpoint(db_session)
    db_session.commit()

    plan = _plan(category=15)
    plan["datasets"][0]["filters"].extend(
        [
            {
                "field": {
                    "entity_type_id": 2,
                    "field_code": "UF_CRM_5ECE25C5D78E0",
                    "source_alias": "deal",
                },
                "op": "eq",
                "value": "<ID региона Санкт-Петербурга>",
            },
            {
                "field": {
                    "entity_type_id": 2,
                    "field_code": "UF_CRM_5ECE25C5D78E0",
                    "source_alias": "deal",
                },
                "op": "eq",
                "value": 1107,
            },
        ]
    )

    cid = client.post(f"{API}/conversations", json={"title": "Region placeholder"}).json()["id"]
    version_id = client.post(f"{API}/conversations/{cid}/plan", json={"plan": plan}).json()["version"]["id"]

    preview = client.post(f"{API}/plans/{version_id}/preview")
    assert preview.status_code == 200, preview.text
