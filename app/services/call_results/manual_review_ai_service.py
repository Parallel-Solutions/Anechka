"""LLM helpers for manual review preview (keywords, contact, todo)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from openai import APIConnectionError, APITimeoutError, RateLimitError
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings, get_call_results_model, get_llm_provider_label
from app.services.llm_client import make_openai_client
from app.services.call_results.llm_schema import AlternateContactData

logger = logging.getLogger(__name__)


class SearchKeywordsResult(BaseModel):
    keywords: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class TodoContentResult(BaseModel):
    title: str = Field(max_length=200)
    description: str = Field(max_length=4000)


@dataclass
class AiExtractOutcome:
    keywords: SearchKeywordsResult | None = None
    contact: AlternateContactData | None = None
    todo: TodoContentResult | None = None
    error_type: str | None = None
    error_message: str | None = None


_KEYWORDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
        },
        "confidence": {"type": "number"},
    },
    "required": ["keywords", "confidence"],
    "additionalProperties": False,
}

_CONTACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "phone": {"type": ["string", "null"]},
        "extension": {"type": ["string", "null"]},
        "email": {"type": ["string", "null"]},
        "position": {"type": ["string", "null"]},
    },
    "required": ["name", "phone", "extension", "email", "position"],
    "additionalProperties": False,
}

_TODO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
    },
    "required": ["title", "description"],
    "additionalProperties": False,
}


class ManualReviewAiService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = get_call_results_model(settings)
        self.provider = get_llm_provider_label(settings)
        self.client = make_openai_client(settings)

    @property
    def enabled(self) -> bool:
        return bool(self.client and self.settings.llm_call_results_enabled)

    def extract_search_keywords(self, transcript: str) -> AiExtractOutcome:
        if not self.enabled or not transcript.strip():
            return AiExtractOutcome(error_type="disabled", error_message="LLM недоступна")
        system = (
            "Из транскрибации телефонного разговора извлеки ключевые слова для поиска другого контакта "
            "в CRM: отдел, должность, ФИО, название подразделения. "
            "Верни 1–5 коротких фраз на русском языке, пригодных для поиска по полям контакта."
        )
        return self._call(system, transcript, "search_keywords", _KEYWORDS_SCHEMA, SearchKeywordsResult, "keywords")

    def extract_contact_data(self, transcript: str) -> AiExtractOutcome:
        if not self.enabled or not transcript.strip():
            return AiExtractOutcome(error_type="disabled", error_message="LLM недоступна")
        system = (
            "Из транскрибации телефонного разговора извлеки данные нового контакта, "
            "которого нужно завести в CRM: имя, телефон, должность, email, добавочный. "
            "Если поле не упоминается — null."
        )
        return self._call(system, transcript, "extract_contact", _CONTACT_SCHEMA, AlternateContactData, "contact")

    def generate_todo_content(self, transcript: str, *, deal_title: str = "") -> AiExtractOutcome:
        if not self.enabled or not transcript.strip():
            return AiExtractOutcome(error_type="disabled", error_message="LLM недоступна")
        context = f"Сделка: {deal_title}\n\n" if deal_title else ""
        system = (
            "По транскрибации положительного телефонного разговора сформируй CRM-дело для менеджера: "
            "краткий заголовок (до 120 символов) и описание с итогом разговора и следующими шагами."
        )
        return self._call(system, context + transcript, "generate_todo", _TODO_SCHEMA, TodoContentResult, "todo")

    def _call(
        self,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        model_cls: type[BaseModel],
        result_field: str,
    ) -> AiExtractOutcome:
        if not self.client:
            return AiExtractOutcome(error_type="config", error_message="LLM API key не настроен")

        max_retries = self.settings.llm_call_results_max_retries
        last_error: str | None = None

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user[:12000]},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                    timeout=self.settings.llm_call_results_timeout_seconds,
                    store=False,
                )
                raw = response.choices[0].message.content or "{}"
                data = json.loads(raw)
                parsed = model_cls.model_validate(data)
                if result_field == "keywords":
                    return AiExtractOutcome(keywords=parsed)
                if result_field == "contact":
                    return AiExtractOutcome(contact=parsed)
                return AiExtractOutcome(todo=parsed)
            except (APITimeoutError, RateLimitError, APIConnectionError) as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    continue
            except Exception as exc:
                logger.exception("Manual review AI call failed")
                return AiExtractOutcome(error_type="error", error_message=str(exc))

        return AiExtractOutcome(error_type="error", error_message=last_error or "LLM error")
