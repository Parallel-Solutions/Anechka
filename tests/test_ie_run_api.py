"""Phase F: run lifecycle API, synchronous job runner, guarded download."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.models import CrmEntity, ENTITY_DEAL, SyncCheckpoint
from app.repositories.intelligent_export_repository import IntelligentExportRepository, ScopeContext
from app.services.auth_service import AuthService, resolve_portal_id
from app.services.intelligent_export.job_runner import run_intelligent_export_job
from app.services.intelligent_export.plan_service import prepare_plan, validation_to_dict
from app.services.intelligent_export.scope import build_scope

API = "/api/intelligent-export"


def _portal():
    return resolve_portal_id(get_settings())


def _seed(db, n=2):
    for i in range(1, n + 1):
        db.add(
            CrmEntity(
                portal_id=_portal(),
                entity_type_id=ENTITY_DEAL,
                entity_id=i,
                title=f"Deal {i}",
                category_id=1,
                payload_hash=f"h{i}",
                raw_payload={"id": i, "TITLE": f"Deal {i}"},
            )
        )
    db.add(
        SyncCheckpoint(
            portal_id=_portal(),
            resource_name="deal",
            entity_type_id=ENTITY_DEAL,
            last_successful_sync_at=datetime.now(timezone.utc),
        )
    )


def _plan():
    return {
        "schema_version": "2.0",
        "title": "Сделки",
        "datasets": [
            {"id": "deals", "primary_entity_type_id": 2, "sources": [{"alias": "deal", "entity_type_id": 2}], "limit": 100}
        ],
        "workbook": {
            "format": "xlsx",
            "filename_label": "deals_run",
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


def _save_version(db, user, plan_dict):
    scope = build_scope(user, get_settings())
    repo = IntelligentExportRepository(db, ScopeContext(user=user, portal_id=_portal()))
    conv = repo.create_conversation("run test")
    prepared = prepare_plan(db, _portal(), scope, plan_dict)
    version = repo.save_plan_version(
        conv.id,
        plan_json=prepared.plan.model_dump(mode="json"),
        validation_result_json=validation_to_dict(prepared.validation, status="valid"),
        catalog_snapshot_hash=prepared.catalog_hash,
    )
    return repo, conv, version


@pytest.fixture()
def no_exec(monkeypatch):
    """Prevent the job executor from running threads during API tests."""

    class _Dummy:
        def submit(self, *a, **k):
            return None

    from app.services.job_service import JobService

    monkeypatch.setattr(JobService, "get_executor", classmethod(lambda cls, *a, **k: _Dummy()))


def test_run_endpoint_creates_run_and_job(client, db_session, no_exec):
    _seed(db_session)
    db_session.commit()
    cid = client.post(f"{API}/conversations", json={}).json()["id"]
    version_id = client.post(f"{API}/conversations/{cid}/plan", json={"plan": _plan()}).json()["version"]["id"]

    resp = client.post(f"{API}/plans/{version_id}/run")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["run_id"] and body["job_id"]

    run = client.get(f"{API}/runs/{body['run_id']}").json()
    assert run["id"] == body["run_id"]


def test_job_runner_end_to_end(db_session):
    user = AuthService(get_settings(), db_session).ensure_default_ie_user()
    _seed(db_session, n=3)
    db_session.commit()
    repo, conv, version = _save_version(db_session, user, _plan())
    run = repo.create_run(plan_version_id=version.id, conversation_id=conv.id, status="running")

    path = run_intelligent_export_job(
        db_session,
        get_settings(),
        {"portal_id": _portal(), "user_id": user.id, "plan_version_id": version.id, "run_id": run.id},
        cancel_check=lambda: False,
        progress=lambda c, t, s: None,
        log=lambda m: None,
    )
    db_session.refresh(run)
    assert run.status == "completed"
    assert run.row_count == 3
    assert Path(path).exists()
    Path(path).unlink(missing_ok=True)


def test_download_after_completed_run(client, db_session):
    user = AuthService(get_settings(), db_session).ensure_default_ie_user()
    _seed(db_session)
    db_session.commit()

    repo, conv, version = _save_version(db_session, user, _plan())
    run = repo.create_run(plan_version_id=version.id, conversation_id=conv.id, status="running")
    path = run_intelligent_export_job(
        db_session,
        get_settings(),
        {"portal_id": _portal(), "user_id": user.id, "plan_version_id": version.id, "run_id": run.id},
        cancel_check=lambda: False,
        progress=lambda c, t, s: None,
        log=lambda m: None,
    )

    ok = client.get(f"{API}/runs/{run.id}/download")
    assert ok.status_code == 200
    assert len(ok.content) > 0

    Path(path).unlink(missing_ok=True)
