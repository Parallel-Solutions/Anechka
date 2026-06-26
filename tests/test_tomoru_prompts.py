"""Tests for Tomoru chat prompt library."""

from __future__ import annotations

from app.services.intelligent_export.tomoru_prompts import (
    get_tomoru_prompt,
    has_tomoru_trigger,
    list_dialog_starters,
    list_tomoru_prompts,
)

API = "/api/intelligent-export"


def test_list_has_nineteen_prompts():
    prompts = list_tomoru_prompts()
    assert len(prompts) == 19


def test_all_step_one_prompts_have_tomoru_trigger():
    for p in list_dialog_starters():
        assert has_tomoru_trigger(p.prompt), p.id


def test_two_step_scenarios_have_follow_up():
    two_step = [p for p in list_dialog_starters() if p.category == "two_step"]
    assert len(two_step) == 3
    for p in two_step:
        assert p.step == 1
        assert p.scenario_id
        assert p.follow_up_prompt


def test_unique_ids():
    ids = [p["id"] for p in list_tomoru_prompts()]
    assert len(ids) == len(set(ids))


def test_get_prompt_unknown():
    assert get_tomoru_prompt("missing") is None


def test_prompt_dict_shape():
    sample = list_tomoru_prompts()[0]
    for key in ("id", "category", "title", "prompt", "purpose", "preview_checks"):
        assert key in sample


def test_chat_prompts_endpoint(client):
    resp = client.get(f"{API}/chat-prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["prompts"]) == 19
    assert data["prompts"][0]["title"]


def test_from_prompt_not_found(client):
    resp = client.post(f"{API}/conversations/from-prompt/unknown-id")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "PROMPT_NOT_FOUND"


def test_from_prompt_creates_conversation_and_messages(client, db_session):
    from app.config import get_settings
    from app.models import CrmEntity, ENTITY_DEAL
    from app.routers.intelligent_export import get_planner
    from app.services.auth_service import resolve_portal_id
    from app.services.intelligent_export.planner import FakePlanner

    db_session.add(
        CrmEntity(
            portal_id=resolve_portal_id(get_settings()),
            entity_type_id=ENTITY_DEAL,
            entity_id=1,
            title="Deal 1",
            category_id=15,
            payload_hash="h1",
            raw_payload={"id": 1, "TITLE": "Deal 1", "CATEGORY_ID": 15},
        )
    )
    db_session.commit()

    plan = {
        "schema_version": "2.0",
        "title": "Tomoru",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": 2,
                "sources": [{"alias": "deal", "entity_type_id": 2}],
            }
        ],
        "workbook": {
            "format": "xlsx",
            "sheets": [
                {
                    "id": "s",
                    "name": "Номера",
                    "mode": "rows",
                    "dataset_id": "deals",
                    "columns": [],
                }
            ],
        },
    }
    fake = FakePlanner(responses=[{"status": "candidate_ready", "assistant_message": "Готово", "plan": plan}])
    client.app.dependency_overrides[get_planner] = lambda: fake

    try:
        resp = client.post(f"{API}/conversations/from-prompt/region_moscow").json()
        assert resp["conversation_id"]
        assert resp["title"] == "Tomoru — Москва"
        assert resp["status"] == "validated"
        msgs = client.get(f"{API}/conversations/{resp['conversation_id']}/messages").json()["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert "туморо" in msgs[0]["content"].lower() or "tomoru" in msgs[0]["content"].lower()
    finally:
        client.app.dependency_overrides.pop(get_planner, None)


def test_from_prompt_two_step_returns_follow_up(client, db_session):
    from app.config import get_settings
    from app.models import CrmEntity, ENTITY_DEAL
    from app.routers.intelligent_export import get_planner
    from app.services.auth_service import resolve_portal_id
    from app.services.intelligent_export.planner import FakePlanner

    db_session.add(
        CrmEntity(
            portal_id=resolve_portal_id(get_settings()),
            entity_type_id=ENTITY_DEAL,
            entity_id=2,
            title="Deal 2",
            category_id=15,
            payload_hash="h2",
            raw_payload={"id": 2, "TITLE": "Deal 2", "CATEGORY_ID": 15},
        )
    )
    db_session.commit()

    plan = {
        "schema_version": "2.0",
        "title": "Tomoru base",
        "datasets": [{"id": "deals", "primary_entity_type_id": 2, "sources": [{"alias": "deal", "entity_type_id": 2}]}],
        "workbook": {"format": "xlsx", "sheets": [{"id": "s", "name": "Номера", "mode": "rows", "dataset_id": "deals", "columns": []}]},
    }
    fake = FakePlanner(responses=[{"status": "needs_clarification", "assistant_message": "Уточните регион", "clarifying_questions": []}])
    client.app.dependency_overrides[get_planner] = lambda: fake

    try:
        resp = client.post(f"{API}/conversations/from-prompt/twostep_region").json()
        assert resp["follow_up_prompt"] == "Только Томская область"
    finally:
        client.app.dependency_overrides.pop(get_planner, None)
