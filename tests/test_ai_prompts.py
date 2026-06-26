"""Tests for AI prompt templates API."""

from app.services.ai_prompt_service import DEFAULT_PROMPTS


def test_list_prompts_seeds_defaults(client):
    resp = client.get("/api/ai/prompts")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == len(DEFAULT_PROMPTS)
    assert data[0]["title"] == DEFAULT_PROMPTS[0][0]
    assert data[0]["prompt"] == DEFAULT_PROMPTS[0][1]


def test_create_prompt(client):
    resp = client.post(
        "/api/ai/prompts",
        json={"title": "Тест", "prompt": "Покажи все сделки"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Тест"
    assert body["prompt"] == "Покажи все сделки"
    assert body["id"] > 0


def test_update_prompt(client):
    create = client.post(
        "/api/ai/prompts",
        json={"title": "Старое", "prompt": "Старый текст"},
    ).json()
    resp = client.put(
        f"/api/ai/prompts/{create['id']}",
        json={"title": "Новое", "prompt": "Новый текст"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Новое"
    assert body["prompt"] == "Новый текст"


def test_delete_prompt(client):
    create = client.post(
        "/api/ai/prompts",
        json={"title": "Удалить", "prompt": "Текст"},
    ).json()
    resp = client.delete(f"/api/ai/prompts/{create['id']}")
    assert resp.status_code == 200
    assert resp.json()["message"] == "Промпт удалён"

    again = client.delete(f"/api/ai/prompts/{create['id']}")
    assert again.status_code == 404


def test_create_prompt_validation(client):
    resp = client.post("/api/ai/prompts", json={"title": "", "prompt": "x"})
    assert resp.status_code == 422

    resp = client.post("/api/ai/prompts", json={"title": "x", "prompt": ""})
    assert resp.status_code == 422
