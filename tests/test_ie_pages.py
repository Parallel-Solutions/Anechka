"""Phase G: intelligent export UI page renders and assets are served."""

from __future__ import annotations


def test_intelligent_export_page_renders(client):
    resp = client.get("/intelligent-export")
    assert resp.status_code == 200
    assert "ie-app" in resp.text
    assert "d-flex flex-row" in resp.text
    assert "ie-fullpage" in resp.text
    assert "ie-sidebar-label" in resp.text
    assert "История" in resp.text
    assert "ie-conv-list" in resp.text
    assert "ie-messages" in resp.text
    assert "ie-composer" in resp.text
    assert "ie-plan-modal" in resp.text
    assert "ie-preview-modal" in resp.text
    assert "Быстрый экспорт" not in resp.text
    assert "/static/js/intelligent_export.js" in resp.text
    assert "ie-login" not in resp.text


def test_intelligent_export_js_served(client):
    resp = client.get("/static/js/intelligent_export.js")
    assert resp.status_code == 200
    assert "loadConversations" in resp.text
    assert "openPlanModal" in resp.text
    assert "showEmptyState" in resp.text
    assert "ie-starter-btn" in resp.text
    assert "selectConversation" in resp.text
    assert "loadTemplates" not in resp.text
    assert "checkAuth" not in resp.text


def test_intelligent_export_css_layout(client):
    resp = client.get("/static/css/app.css")
    assert resp.status_code == 200
    assert "#ie-app.ie-page-layout" in resp.text
    assert "min-height: 0" in resp.text
    assert "flex-direction: row" in resp.text


def test_nav_has_intelligent_export_link(client):
    resp = client.get("/")
    assert "Умные выгрузки" in resp.text
    assert 'href="/"' in resp.text
