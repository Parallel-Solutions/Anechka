"""Phase E: chat endpoint integration with a deterministic fake planner."""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models import CrmEntity, ENTITY_DEAL
from app.routers.intelligent_export import get_planner
from app.services.auth_service import resolve_portal_id
from app.services.intelligent_export.planner import FakePlanner

API = "/api/intelligent-export"


def _seed_data(db):
    """Minimal CRM data so the chat preflight (NO_DATA guard) passes."""
    db.add(
        CrmEntity(
            portal_id=resolve_portal_id(get_settings()),
            entity_type_id=ENTITY_DEAL,
            entity_id=1,
            title="Deal 1",
            category_id=1,
            payload_hash="h1",
            raw_payload={"id": 1, "TITLE": "Deal 1"},
        )
    )


def _valid_plan():
    return {
        "schema_version": "2.0",
        "title": "Сделки за период",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": 2,
                "sources": [{"alias": "deal", "entity_type_id": 2}],
                "filters": [{"field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"}, "op": "gte", "value": "@today-30d"}],
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
                        {"id": "idc", "header": "ID", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"}}}
                    ],
                }
            ],
        },
    }


def _invalid_plan():
    plan = _valid_plan()
    plan["workbook"]["sheets"][0]["columns"].append(
        {
            "id": "bad",
            "header": "X",
            "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "NO_FIELD", "source_alias": "deal"}},
        }
    )
    return plan


@pytest.fixture()
def use_fake_planner(client):
    holder = {}

    def _install(responses):
        fake = FakePlanner(responses=responses)
        holder["fake"] = fake
        client.app.dependency_overrides[get_planner] = lambda: fake
        return fake

    yield _install
    client.app.dependency_overrides.pop(get_planner, None)


def test_chat_needs_clarification(client, db_session, use_fake_planner):
    _seed_data(db_session)
    db_session.commit()
    use_fake_planner([{"status": "needs_clarification", "assistant_message": "Какой период?", "clarifying_questions": ["Период?"]}])
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    resp = client.post(f"{API}/conversations/{cid}/chat", json={"message": "выгрузи сделки"}).json()
    assert resp["status"] == "needs_clarification"
    assert resp["version"] is None
    msgs = client.get(f"{API}/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_chat_validated_saves_plan_version_and_resolves_dates(client, db_session, use_fake_planner):
    _seed_data(db_session)
    db_session.commit()
    use_fake_planner([{"status": "candidate_ready", "assistant_message": "Готово", "plan": _valid_plan()}])
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    resp = client.post(f"{API}/conversations/{cid}/chat", json={"message": "сделки за месяц"}).json()
    assert resp["status"] == "validated"
    assert resp["version"] is not None
    value = resp["plan"]["datasets"][0]["filters"][0]["value"]
    assert value.startswith("20") and "-" in value and not value.startswith("@")
    plans = client.get(f"{API}/conversations/{cid}/plans").json()["plans"]
    assert len(plans) == 1 and plans[0]["valid"] is True


def test_chat_records_used_memory(client, db_session, use_fake_planner):
    _seed_data(db_session)
    db_session.commit()
    mem = client.post(
        f"{API}/memory/proposals",
        json={"scope": "project", "kind": "term", "key": "регион", "content": "город"},
    ).json()["memory"]
    assert mem["status"] == "approved"

    plan = _valid_plan()
    use_fake_planner([{"status": "candidate_ready", "assistant_message": "ок", "plan": plan, "used_memory_ids": [mem["id"]]}])
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    resp = client.post(f"{API}/conversations/{cid}/chat", json={"message": "сделки по региону"}).json()
    assert resp["status"] == "validated"
    used = resp["validation"]["memory_used"]
    assert any(ref["memory_id"] == mem["id"] for ref in used)


def test_chat_works_without_login(client, db_session, use_fake_planner):
    db_session.commit()
    use_fake_planner([{"status": "needs_clarification", "assistant_message": "x"}])
    cid_resp = client.post(f"{API}/conversations", json={})
    assert cid_resp.status_code == 200


def test_chat_rejected_returns_fix_suggestions(client, db_session, use_fake_planner):
    _seed_data(db_session)
    db_session.commit()
    use_fake_planner([{"status": "candidate_ready", "assistant_message": "x", "plan": _invalid_plan()}])
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    resp = client.post(f"{API}/conversations/{cid}/chat", json={"message": "сделки"}).json()
    assert resp["status"] == "rejected"
    assert resp["version"] is None
    assert resp["fix_suggestions"]
    assert any("NO_FIELD" in s for s in resp["fix_suggestions"])
    assert "2 попытки" in resp["assistant_message"]
    msgs = client.get(f"{API}/conversations/{cid}/messages").json()["messages"]
    assistant = msgs[-1]
    assert assistant["role"] == "assistant"
    assert assistant["metadata"]["fix_suggestions"]
    assert "2 попытки" in assistant["content"]
