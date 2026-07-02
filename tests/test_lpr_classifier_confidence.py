"""Tests for OpenAI LPR classifier confidence parsing."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.config import Settings
from app.models import CrmContact
from app.services.intelligent_export.contact_lpr_classifier import (
    KeywordLprClassifier,
    OpenAIContactLprClassifier,
    _parse_confidence,
)
from app.services.intelligent_export.contact_phone_heuristic import ContactCandidate
from app.services.lpr_service import LprConfig


def test_parse_confidence_clamps():
    assert _parse_confidence(150) == 100.0
    assert _parse_confidence(-5) == 0.0
    assert _parse_confidence("72") == 72.0
    assert _parse_confidence("bad") is None


def test_openai_classifier_returns_llm_confidence():
    settings = Settings(openai_api_key="test-key")
    fallback = KeywordLprClassifier(LprConfig(keywords=["директор"], fields=["POST"], stopwords=[]))
    classifier = OpenAIContactLprClassifier(settings, fallback)
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {"contact_id": 5, "reason": "Генеральный директор", "confidence": 88},
                        ensure_ascii=False,
                    )
                )
            )
        ]
    )
    classifier._client = mock_client
    candidate = ContactCandidate(
        contact=CrmContact(portal_id="test", contact_id=5, post="Генеральный директор"),
    )
    result = classifier.pick_lpr([candidate], deal_title="Сделка")
    assert result.contact_id == 5
    assert result.method == "llm"
    assert result.confidence == 88.0
