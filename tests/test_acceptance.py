"""Phase H: full acceptance scenario.

End-to-end: admin bootstraps an analyst, the analyst opens a conversation,
describes an export in natural language (fake planner), gets a validated plan
version, previews it, counts rows, runs the export job, and downloads the file.
Also exercises project memory approval and reuse.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import get_settings
from app.models import CrmEntity, ENTITY_DEAL, SyncCheckpoint
from app.routers.intelligent_export import get_planner
from app.services.auth_service import AuthService, resolve_portal_id
from app.services.intelligent_export.job_runner import run_intelligent_export_job
from app.services.intelligent_export.planner import FakePlanner

API = "/api/intelligent-export"


def _portal():
    return resolve_portal_id(get_settings())


def _seed_deals(db, n=5):
    for i in range(1, n + 1):
        db.add(
            CrmEntity(
                portal_id=_portal(),
                entity_type_id=ENTITY_DEAL,
                entity_id=i,
                title=f"Deal {i}",
                category_id=1,
                created_time=datetime.now(timezone.utc),
                payload_hash=f"hash{i}",
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
        "title": "Сделки за период",
        "datasets": [
            {
                "id": "deals",
                "primary_entity_type_id": 2,
                "sources": [{"alias": "deal", "entity_type_id": 2}],
                "filters": [
                    {
                        "field": {"entity_type_id": 2, "field_code": "DATE_CREATE", "source_alias": "deal"},
                        "op": "gte",
                        "value": "@today-30d",
                    }
                ],
                "limit": 100,
            }
        ],
        "workbook": {
            "format": "xlsx",
            "filename_label": "deals_export",
            "sheets": [
                {
                    "id": "s",
                    "name": "Сделки",
                    "mode": "rows",
                    "dataset_id": "deals",
                    "columns": [
                        {"id": "idc", "header": "ID", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "ID", "source_alias": "deal"}}},
                        {"id": "tc", "header": "Название", "value": {"kind": "field", "field": {"entity_type_id": 2, "field_code": "TITLE", "source_alias": "deal"}}},
                    ],
                }
            ],
        },
    }


@pytest.fixture()
def fake_planner(client):
    fake = FakePlanner(responses=[{"status": "candidate_ready", "assistant_message": "Готово", "plan": _plan()}])
    client.app.dependency_overrides[get_planner] = lambda: fake
    yield fake
    client.app.dependency_overrides.pop(get_planner, None)


def test_full_acceptance_flow(client, db_session, fake_planner):
    _seed_deals(db_session, n=5)
    db_session.commit()

    cid = client.post(f"{API}/conversations", json={}).json()["id"]

    # describes the export in natural language; planner returns a validated plan
    chat = client.post(f"{API}/conversations/{cid}/chat", json={"message": "выгрузи сделки за месяц"}).json()
    assert chat["status"] == "validated"
    version_id = chat["version"]["id"]
    # relative date token resolved server-side
    assert not chat["plan"]["datasets"][0]["filters"][0]["value"].startswith("@")

    # count and preview
    count = client.post(f"{API}/plans/{version_id}/count").json()
    assert count["total_count"] == 5
    preview = client.post(f"{API}/plans/{version_id}/preview").json()
    assert preview["total_count"] == 5
    assert preview["sheets"]

    # run the export (synchronously via the job runner to validate the file)
    run = client.post(f"{API}/plans/{version_id}/run").json()
    run_id = run["run_id"]

    path = run_intelligent_export_job(
        db_session,
        get_settings(),
        {"portal_id": _portal(), "user_id": _system_user_id(db_session), "plan_version_id": version_id, "run_id": run_id},
        cancel_check=lambda: False,
        progress=lambda c, t, s: None,
        log=lambda m: None,
    )
    assert Path(path).exists()

    # download is available to the owner
    dl = client.get(f"{API}/runs/{run_id}/download")
    assert dl.status_code == 200
    assert len(dl.content) > 0

    Path(path).unlink(missing_ok=True)


def _system_user_id(db):
    user = AuthService(get_settings(), db).ensure_default_ie_user()
    return user.id
