"""DB-aware memory generator: candidates, idempotency, governance, no leakage."""

from __future__ import annotations

import json

from app.config import get_settings
from app.models import AppUser, CrmEntity, ENTITY_CONTACT, ENTITY_DEAL
from app.repositories.intelligent_export_repository import IntelligentExportRepository, ScopeContext
from app.services.export_plan.catalog import FieldCatalog
from app.services.intelligent_export.db_profiler import EntityProfile, PortalProfile
from app.services.intelligent_export.memory_generator import (
    MemoryCandidate,
    build_candidates,
    generate_memory,
    upsert_candidates,
)

PORTAL = "test.bitrix24.ru"


def _repo(db, role="admin"):
    user = AppUser(portal_id=PORTAL, email=f"{role}@a.ru", password_hash="x", role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return IntelligentExportRepository(db, ScopeContext(user=user, portal_id=PORTAL))


def _empty_entity(etid, total=100):
    return EntityProfile(
        entity_type_id=etid,
        total_seen=total,
        fill_counts={},
        fill_rates={},
        multifield={
            "PHONE": {"present": False, "count": 0, "fill_rate": 0.0},
            "EMAIL": {"present": False, "count": 0, "fill_rate": 0.0},
            "FM": {"present": False, "count": 0, "fill_rate": 0.0},
        },
        fk_link_shares={},
        phone_format=None,
    )


def _profile_with_phone_fm():
    contact = _empty_entity(ENTITY_CONTACT)
    contact.multifield["PHONE"] = {"present": True, "count": 80, "fill_rate": 0.8}
    contact.multifield["FM"] = {"present": True, "count": 90, "fill_rate": 0.9}
    contact.multifield["EMAIL"] = {"present": True, "count": 60, "fill_rate": 0.6}
    return PortalProfile(portal_id=PORTAL, entities={ENTITY_CONTACT: contact})


def test_phone_vs_fm_candidate_built():
    profile = _profile_with_phone_fm()
    catalog = FieldCatalog(portal_id=PORTAL)
    candidates = build_candidates(profile, catalog)
    keys = {c.key for c in candidates}
    assert f"profile:{ENTITY_CONTACT}:phone_vs_fm" in keys
    cand = next(c for c in candidates if c.key.endswith("phone_vs_fm"))
    assert cand.kind == "instruction"
    assert "profile_hash" in cand.value_json


def test_candidates_capped():
    deal = _empty_entity(ENTITY_DEAL)
    deal.fill_rates = {f"F{i}": 0.01 for i in range(30)}
    profile = PortalProfile(portal_id=PORTAL, entities={ENTITY_DEAL: deal})
    candidates = build_candidates(profile, FieldCatalog(portal_id=PORTAL), max_entries=3)
    assert len(candidates) == 3


def test_no_sensitive_value_leakage():
    profile = _profile_with_phone_fm()
    candidates = build_candidates(profile, FieldCatalog(portal_id=PORTAL))
    blob = json.dumps([{"content": c.content, "value_json": c.value_json} for c in candidates], ensure_ascii=False)
    assert "89161112233" not in blob
    assert "@" not in blob  # no e-mail addresses copied


def test_upsert_creates_proposed_import_project(db_session):
    repo = _repo(db_session)
    candidates = build_candidates(_profile_with_phone_fm(), FieldCatalog(portal_id=PORTAL))
    result = upsert_candidates(repo, candidates)
    assert result["created"]
    for entry in result["created"]:
        assert entry.status == "proposed"
        assert entry.source == "import"
        assert entry.scope == "project"


def test_idempotent_rerun_skips_and_updates(db_session):
    repo = _repo(db_session)
    candidates = build_candidates(_profile_with_phone_fm(), FieldCatalog(portal_id=PORTAL))

    first = upsert_candidates(repo, candidates)
    assert first["created"] and not first["updated"]

    # identical re-run -> all skipped, no duplicates
    second = upsert_candidates(repo, candidates)
    assert not second["created"]
    assert not second["updated"]
    assert second["skipped"]

    # changed content under same key -> update with bumped version
    key = candidates[0].key
    changed = MemoryCandidate(
        kind=candidates[0].kind,
        key=key,
        content="Обновлённый факт о телефонах.",
        priority=candidates[0].priority,
        value_json={"metrics": {"x": 1}, "profile_hash": "deadbeef0000"},
    )
    third = upsert_candidates(repo, [changed])
    assert len(third["updated"]) == 1
    updated = third["updated"][0]
    assert updated.version == 2
    assert updated.content == "Обновлённый факт о телефонах."


def test_human_decision_not_overridden(db_session):
    repo = _repo(db_session)
    candidates = build_candidates(_profile_with_phone_fm(), FieldCatalog(portal_id=PORTAL))
    created = upsert_candidates(repo, candidates)["created"]
    entry = created[0]
    repo.update_memory(entry.id, status="approved")

    changed = MemoryCandidate(
        kind=entry.kind,
        key=entry.key,
        content="Совсем другой текст.",
        priority=entry.priority,
        value_json={"metrics": {"y": 2}, "profile_hash": "feedface0001"},
    )
    result = upsert_candidates(repo, [changed])
    assert not result["updated"]
    assert result["skipped"]
    refreshed = repo.get_memory(entry.id)
    assert refreshed.status == "approved"
    assert refreshed.content != "Совсем другой текст."


def test_generate_memory_end_to_end(db_session):
    repo = _repo(db_session)
    for eid in range(1, 4):
        db_session.add(
            CrmEntity(
                portal_id=PORTAL,
                entity_type_id=ENTITY_CONTACT,
                entity_id=eid,
                title=f"C{eid}",
                payload_hash=f"c{eid}",
                raw_payload={
                    "id": eid,
                    "PHONE": [{"VALUE": "89161112233", "VALUE_TYPE": "WORK"}],
                    "FM": [{"value": "x@y.ru", "typeId": "EMAIL"}],
                },
            )
        )
    db_session.commit()

    result = generate_memory(db_session, repo, PORTAL, get_settings())
    keys = {e.key for e in result["created"]}
    assert f"profile:{ENTITY_CONTACT}:phone_vs_fm" in keys
    for entry in result["created"]:
        assert entry.status == "proposed"
        assert entry.source == "import"
