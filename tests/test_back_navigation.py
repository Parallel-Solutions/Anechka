"""Tests for the shared safe back-navigation control."""

from __future__ import annotations


def test_back_button_is_rendered_on_work_page(client):
    response = client.get("/instruction")

    assert response.status_code == 200
    assert 'id="app-back-button"' in response.text
    assert 'data-fallback="/"' in response.text
    assert "← Назад" in response.text


def test_back_button_is_hidden_on_login(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert 'id="app-back-button"' not in response.text


def test_settings_users_has_settings_fallback(client):
    response = client.get("/settings/users")

    assert response.status_code == 200
    assert 'data-fallback="/settings"' in response.text


def test_navigation_script_uses_only_same_origin_history(client):
    response = client.get("/static/js/navigation.js")

    assert response.status_code == 200
    assert "previous.origin === current.origin" in response.text
    assert "window.location.assign(fallback)" in response.text