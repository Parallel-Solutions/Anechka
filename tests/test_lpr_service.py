"""Tests for LPR detection service."""

from __future__ import annotations

import json

from app.models import AppSetting, CrmContact
from app.services.bitrix_import.contact_parser import POST_CUSTOM_FIELD
from app.services.intelligent_export.contact_lpr_classifier import KeywordLprClassifier
from app.services.intelligent_export.contact_phone_heuristic import ContactCandidate
from app.services.lpr_keywords_industry import INDUSTRY_LPR_KEYWORDS
from app.services.lpr_service import (
    DEFAULT_LPR_FIELDS,
    DEFAULT_LPR_KEYWORDS,
    GENERIC_LPR_KEYWORDS,
    LPR_FIELDS_KEY,
    LPR_KEYWORDS_KEY,
    LPR_STOPWORDS_KEY,
    LprConfig,
    contact_to_lpr_dict,
    detect_lpr,
    load_lpr_config,
    lpr_keyword_rank,
    save_lpr_config,
)


def test_default_fields_include_fio_and_uf_post():
    assert "POST" in DEFAULT_LPR_FIELDS
    assert POST_CUSTOM_FIELD in DEFAULT_LPR_FIELDS
    assert "LAST_NAME" in DEFAULT_LPR_FIELDS
    assert "SECOND_NAME" in DEFAULT_LPR_FIELDS


def test_default_keywords_merge_generic_and_industry():
    assert "директор" in DEFAULT_LPR_KEYWORDS
    assert "Главный архитектор" in DEFAULT_LPR_KEYWORDS
    assert len(DEFAULT_LPR_KEYWORDS) > len(GENERIC_LPR_KEYWORDS)
    assert len(INDUSTRY_LPR_KEYWORDS) >= 400
    assert DEFAULT_LPR_KEYWORDS[0] == "Главный архитектор"
    assert DEFAULT_LPR_KEYWORDS.index("директор") > DEFAULT_LPR_KEYWORDS.index("Главный архитектор")


def test_lpr_keyword_rank_prefers_higher_priority_keyword():
    config = LprConfig(
        keywords=["Главный архитектор", "директор"],
        fields=["POST", "LAST_NAME"],
        stopwords=[],
    )
    rank, reason = lpr_keyword_rank(
        {"POST": "Заместитель директора", "LAST_NAME": "Иванов, главный архитектор"},
        config,
    )
    assert rank == 0
    assert "Главный архитектор" in reason


def test_keyword_classifier_picks_highest_priority_contact():
    chief = ContactCandidate(
        contact=CrmContact(portal_id="test", contact_id=1, post="Заместитель директора департамента"),
    )
    gap = ContactCandidate(
        contact=CrmContact(portal_id="test", contact_id=2, post="Главный архитектор города"),
    )
    config = LprConfig(
        keywords=["Главный архитектор города", "директор"],
        fields=["POST"],
        stopwords=[],
    )
    result = KeywordLprClassifier(config).pick_lpr([chief, gap])
    assert result.contact_id == 2
    assert "Главный архитектор города" in result.reason


def test_detect_lpr_from_lowercase_post_key():
    config = LprConfig(keywords=["директор"], fields=["POST"], stopwords=[])
    is_lpr, reason = detect_lpr({"post": "Генеральный директор"}, config)
    assert is_lpr is True
    assert "директор" in reason


def test_detect_lpr_from_last_name_when_post_empty():
    config = LprConfig(keywords=["заместител"], fields=["LAST_NAME"], stopwords=[])
    is_lpr, _reason = detect_lpr(
        {"LAST_NAME": "Пуршева (И.о. заместителя главы района.)"},
        config,
    )
    assert is_lpr is True


def test_detect_lpr_from_post_custom_uf_field():
    config = LprConfig(keywords=["начальник управления"], fields=[POST_CUSTOM_FIELD], stopwords=[])
    contact = CrmContact(
        portal_id="test",
        contact_id=1,
        post_custom="Начальник управления архитектуры",
    )
    is_lpr, _reason = detect_lpr(contact_to_lpr_dict(contact), config)
    assert is_lpr is True


def test_stopword_excludes_lpr():
    config = LprConfig(keywords=["начальник"], fields=["POST"], stopwords=["бывш"])
    is_lpr, _ = detect_lpr({"POST": "бывш начальник отдела"}, config)
    assert is_lpr is False


def test_keyword_classifier_uses_canonical_post():
    contact = CrmContact(
        portal_id="test",
        contact_id=42,
        post="Генеральный директор",
        full_name="Иванов",
    )
    candidate = ContactCandidate(contact=contact)
    classifier = KeywordLprClassifier(LprConfig(keywords=["директор"], fields=["POST"], stopwords=[]))
    result = classifier.pick_lpr([candidate])
    assert result.contact_id == 42
    assert result.reason


def test_contact_to_lpr_dict_merges_columns_and_raw():
    contact = CrmContact(
        portal_id="test",
        contact_id=7,
        post="Директор",
        post_custom="Главный архитектор области",
        last_name="Иванов",
        raw_payload={"COMMENTS": "note"},
    )
    data = contact_to_lpr_dict(contact)
    assert data["POST"] == "Директор"
    assert data[POST_CUSTOM_FIELD] == "Главный архитектор области"
    assert data["LAST_NAME"] == "Иванов"
    assert data["COMMENTS"] == "note"


def test_load_merges_partial_stored_with_defaults(db_session):
    db_session.add(
        AppSetting(
            key=LPR_KEYWORDS_KEY,
            value=json.dumps(["арх", "архитектор", "директор"], ensure_ascii=False),
        )
    )
    db_session.commit()

    config = load_lpr_config(db_session)

    assert len(config.keywords) > len(GENERIC_LPR_KEYWORDS)
    assert "арх" in config.keywords
    assert "архитектор" in config.keywords
    assert "директор" in config.keywords
    assert "Главный архитектор" in config.keywords


def test_save_merges_user_input_with_defaults(db_session):
    config = save_lpr_config(
        db_session,
        keywords=["мой_уникальный_ключ", "директор"],
        fields=list(DEFAULT_LPR_FIELDS),
        stopwords=[],
    )

    assert "мой_уникальный_ключ" in config.keywords
    assert "директор" in config.keywords
    assert len(config.keywords) > len(GENERIC_LPR_KEYWORDS)

    row = db_session.query(AppSetting).filter(AppSetting.key == LPR_KEYWORDS_KEY).one()
    stored = json.loads(row.value)
    assert "мой_уникальный_ключ" in stored
    assert len(stored) > 2


def test_save_does_not_shrink_fields(db_session):
    config = save_lpr_config(
        db_session,
        keywords=["директор"],
        fields=["POST"],
        stopwords=["бывш"],
    )

    assert config.fields == list(DEFAULT_LPR_FIELDS)
    assert POST_CUSTOM_FIELD in config.fields
    assert "LAST_NAME" in config.fields
    assert "SECOND_NAME" in config.fields

    row = db_session.query(AppSetting).filter(AppSetting.key == LPR_FIELDS_KEY).one()
    stored = json.loads(row.value)
    assert stored == list(DEFAULT_LPR_FIELDS)


def test_load_merges_partial_stored_fields_and_stopwords(db_session):
    db_session.add(AppSetting(key=LPR_FIELDS_KEY, value=json.dumps(["POST"])))
    db_session.add(AppSetting(key=LPR_STOPWORDS_KEY, value=json.dumps(["бывш"])))
    db_session.commit()

    config = load_lpr_config(db_session)

    assert config.fields == list(DEFAULT_LPR_FIELDS)
    assert "бывш" in config.stopwords
    assert "не работает" in config.stopwords
