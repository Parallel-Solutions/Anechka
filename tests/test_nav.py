"""Tests for top navbar and settings-area subnav."""

from __future__ import annotations


def _navbar_html(html: str) -> str:
    start = html.index('class="navbar-nav')
    end = html.index("</nav>", start)
    return html[start:end]


def test_navbar_hides_history_and_crm_import(client):
    resp = client.get("/tomoru-export")
    assert resp.status_code == 200
    nav = _navbar_html(resp.text)
    assert 'href="/exports"' not in nav
    assert 'href="/bitrix-import"' not in nav
    assert 'href="/tomoru-export"' in nav
    assert 'href="/call-results"' in nav
    assert 'href="/instruction"' in nav
    assert 'href="/settings"' in nav


def test_instruction_page_renders(client):
    resp = client.get("/instruction")
    assert resp.status_code == 200
    assert "Подробное описание" in resp.text
    assert "Краткая инструкция" in resp.text


def test_settings_subnav_not_on_work_pages(client):
    resp = client.get("/tomoru-export")
    assert resp.status_code == 200
    assert "nav nav-pills mb-4" not in resp.text


def test_settings_subnav_renders(client):
    for path in ("/settings", "/exports", "/bitrix-import"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "nav nav-pills mb-4" in resp.text
        assert 'href="/settings"' in resp.text
        assert 'href="/exports"' in resp.text
        assert 'href="/bitrix-import"' in resp.text


def test_settings_subnav_active_state(client):
    cases = [
        ("/settings", 'class="nav-link active" href="/settings"'),
        ("/exports", 'class="nav-link active" href="/exports"'),
        ("/bitrix-import", 'class="nav-link active" href="/bitrix-import"'),
        ("/bitrix-import/fields", 'class="nav-link active" href="/bitrix-import"'),
    ]
    for path, active_snippet in cases:
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert active_snippet in resp.text, path
