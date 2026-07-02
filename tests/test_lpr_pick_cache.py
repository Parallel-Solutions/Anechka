"""Tests for LPR pick cache."""

from __future__ import annotations

from app.models import CrmContact
from app.services.intelligent_export.contact_phone_heuristic import ContactCandidate
from app.services.lpr_pick_cache import (
    CachedLprPick,
    compute_input_hash,
    get_cached,
    load_all,
    save_all,
    set_cached,
)
from app.services.lpr_service import LprConfig, save_lpr_config


def _candidate(contact_id: int, post: str = "") -> ContactCandidate:
    return ContactCandidate(
        contact=CrmContact(portal_id="test", contact_id=contact_id, post=post),
    )


def test_compute_input_hash_changes_with_contact():
    config = LprConfig(keywords=["директор"], fields=["POST"], stopwords=[])
    base = compute_input_hash("Deal A", [_candidate(1, "Директор")], config)
    changed = compute_input_hash("Deal A", [_candidate(1, "Менеджер")], config)
    assert base != changed


def test_cache_hit_and_miss():
    cache: dict[int, CachedLprPick] = {}
    input_hash = "abc123"
    assert get_cached(cache, 42, input_hash) is None
    set_cached(
        cache,
        42,
        input_hash=input_hash,
        contact_id=7,
        reason="ЛПР",
        confidence=85.0,
    )
    hit = get_cached(cache, 42, input_hash)
    assert hit is not None
    assert hit.contact_id == 7
    assert hit.confidence == 85.0
    assert get_cached(cache, 42, "other-hash") is None


def test_save_and_load_roundtrip(db_session):
    portal = "cache-roundtrip"
    cache = {
        10: CachedLprPick(
            input_hash="hash10",
            contact_id=100,
            reason="OpenAI LPR",
            confidence=92.0,
        )
    }
    save_all(db_session, portal, cache)
    loaded = load_all(db_session, portal)
    assert loaded[10].contact_id == 100
    assert loaded[10].confidence == 92.0


def test_save_lpr_config_clears_pick_cache(db_session):
    portal = "cache-clear"
    cache: dict[int, CachedLprPick] = {}
    set_cached(cache, 1, input_hash="h1", contact_id=2, reason="test", confidence=80.0)
    save_all(db_session, portal, cache)
    save_lpr_config(db_session, keywords=["директор"], fields=["POST"], stopwords=[])
    assert load_all(db_session, portal) == {}
