"""OpenAI-based LPR selection among deal contacts (Tomoru heuristic step 2)."""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings, get_planner_model
from app.services.lpr_service import LprConfig, detect_lpr

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты анализируешь контакты CRM сделки и выбираешь одного человека —
лицо, принимающее решение (ЛПР): директор, руководитель, собственник, начальник
и т.п. Не выбирай архитекторов (если есть явная должность «архитектор» — это не ЛПР).
Не выбирай контактов с признаками «бывший», «уволен», «не работает».
Ответь ТОЛЬКО JSON: {"contact_id": <int|null>, "reason": "<кратко на русском>"}.
Если подходящего ЛПР нет — contact_id: null."""


@dataclass
class LprPickResult:
    contact_id: int | None
    reason: str


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
        for cand in candidates:
            payload = cand.to_classifier_dict()
            is_lpr, reason = detect_lpr(payload, self.config)
            if is_lpr:
                return LprPickResult(contact_id=cand.contact_id, reason=reason or "keyword LPR")
        return LprPickResult(contact_id=None, reason="")


class OpenAIContactLprClassifier(ContactLprClassifier):
    def __init__(self, settings: Settings, fallback: ContactLprClassifier):
        self.settings = settings
        self.fallback = fallback
        self.model = get_planner_model(settings)
        self._client = None
        if settings.openai_api_key:
            from openai import OpenAI

            timeout = max(5.0, float(settings.ie_planner_timeout_seconds))
            self._client = OpenAI(api_key=settings.openai_api_key, timeout=timeout)

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
            if cid is not None:
                cid = int(cid)
                if any(c.contact_id == cid for c in candidates):
                    return LprPickResult(contact_id=cid, reason=reason or "OpenAI LPR")
        except Exception as exc:
            logger.warning("OpenAI LPR classifier failed: %s", exc)

        return self.fallback.pick_lpr(candidates, deal_title=deal_title)


def build_lpr_classifier(settings: Settings, lpr_config: LprConfig, *, use_llm: bool) -> ContactLprClassifier:
    fallback = KeywordLprClassifier(lpr_config)
    if not use_llm:
        return fallback
    return OpenAIContactLprClassifier(settings, fallback)
