"""OpenAI-based LPR selection among deal contacts (Tomoru heuristic step 2)."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.config import Settings, get_planner_model
from app.services.llm_client import make_openai_client
from app.services.lpr_service import LprConfig, lpr_keyword_rank

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты анализируешь контакты CRM сделки и выбираешь одного человека —
лицо, принимающее решение (ЛПР): директор, руководитель, собственник, начальник
и т.п. Не выбирай архитекторов (если есть явная должность «архитектор» — это не ЛПР).
Не выбирай контактов с признаками «бывший», «уволен», «не работает».
Ответь ТОЛЬКО JSON: {"contact_id": <int|null>, "reason": "<кратко на русском>", "confidence": <int 0-100>}.
confidence — насколько ты уверен в выборе ЛПР (0 = совсем не уверен, 100 = абсолютно уверен).
Если подходящего ЛПР нет — contact_id: null, confidence: 0."""


def _parse_confidence(value: Any) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, num))


@dataclass
class LprPickResult:
    contact_id: int | None
    reason: str
    confidence: float | None = None
    method: Literal["llm", "keyword"] | None = None


class ContactProfile(Protocol):
    contact_id: int

    def to_classifier_dict(self) -> dict[str, Any]: ...


class ContactLprClassifier(ABC):
    @abstractmethod
    def pick_lpr(self, candidates: list[ContactProfile], *, deal_title: str = "") -> LprPickResult:
        raise NotImplementedError


class KeywordLprClassifier(ContactLprClassifier):
    """Fallback: keyword-based detect_lpr from lpr_service."""

    def __init__(self, config: LprConfig):
        self.config = config

    def pick_lpr(self, candidates: list[ContactProfile], *, deal_title: str = "") -> LprPickResult:
        best: ContactProfile | None = None
        best_rank: int | None = None
        best_reason = ""
        for cand in candidates:
            payload = cand.to_classifier_dict()
            rank, reason = lpr_keyword_rank(payload, self.config)
            if rank is None:
                continue
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best = cand
                best_reason = reason or "keyword LPR"
        if best is not None:
            return LprPickResult(
                contact_id=best.contact_id,
                reason=best_reason,
                method="keyword",
            )
        return LprPickResult(contact_id=None, reason="", method="keyword")


class OpenAIContactLprClassifier(ContactLprClassifier):
    def __init__(self, settings: Settings, fallback: ContactLprClassifier):
        self.settings = settings
        self.fallback = fallback
        self.model = get_planner_model(settings)
        self._client = None
        if settings.openai_api_key:
            timeout = max(5.0, float(settings.ie_planner_timeout_seconds))
            self._client = make_openai_client(settings, timeout=timeout)

    def pick_lpr(self, candidates: list[ContactProfile], *, deal_title: str = "") -> LprPickResult:
        if not candidates:
            return LprPickResult(contact_id=None, reason="")
        if self._client is None:
            return self.fallback.pick_lpr(candidates, deal_title=deal_title)

        profiles = [c.to_classifier_dict() for c in candidates]
        user_payload = {
            "deal_title": deal_title,
            "contacts": profiles,
        }
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = (response.choices[0].message.content or "").strip()
            data = json.loads(raw)
            cid = data.get("contact_id")
            reason = str(data.get("reason") or "")
            confidence = _parse_confidence(data.get("confidence"))
            if cid is not None:
                cid = int(cid)
                if any(c.contact_id == cid for c in candidates):
                    return LprPickResult(
                        contact_id=cid,
                        reason=reason or "OpenAI LPR",
                        confidence=confidence,
                        method="llm",
                    )
        except Exception as exc:
            logger.warning("OpenAI LPR classifier failed: %s", exc)

        return self.fallback.pick_lpr(candidates, deal_title=deal_title)


def build_lpr_classifier(settings: Settings, lpr_config: LprConfig, *, use_llm: bool) -> ContactLprClassifier:
    fallback = KeywordLprClassifier(lpr_config)
    if not use_llm:
        return fallback
    return OpenAIContactLprClassifier(settings, fallback)
