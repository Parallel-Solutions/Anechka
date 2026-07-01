"""LLM classifier gateway — ABC and OpenAI implementation."""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from openai import APIConnectionError, APITimeoutError, OpenAI, RateLimitError
from pydantic import ValidationError

from app.config import Settings, get_call_results_model
from app.services.call_results.classification_prompt import CallResultClassificationPromptBuilder
from app.services.call_results.llm_schema import (
    SCHEMA_VERSION,
    CallResultLLMResult,
)

logger = logging.getLogger(__name__)


@dataclass
class ClassifyOutcome:
    result: CallResultLLMResult | None
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: int = 0
    token_usage: dict[str, int] | None = None
    provider: str = "openai"
    model: str = ""


class BaseCallResultClassifier(ABC):
    @abstractmethod
    def classify(self, input_data: dict[str, Any]) -> ClassifyOutcome:
        ...


class DisabledCallResultClassifier(BaseCallResultClassifier):
    def classify(self, input_data: dict[str, Any]) -> ClassifyOutcome:
        return ClassifyOutcome(result=None, error_type="disabled", error_message="LLM отключена")


class OpenAICallResultClassifier(BaseCallResultClassifier):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model = get_call_results_model(settings)
        self.prompt_builder = CallResultClassificationPromptBuilder()
        self.client = OpenAI(api_key=settings.openai_api_key) if settings.openai_api_key else None

    def classify(self, input_data: dict[str, Any]) -> ClassifyOutcome:
        if not self.client:
            return ClassifyOutcome(result=None, error_type="config", error_message="OpenAI API key не настроен")

        start = time.monotonic()
        last_error: str | None = None
        max_retries = self.settings.llm_call_results_max_retries

        for attempt in range(max_retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.prompt_builder.system_prompt()},
                        {"role": "user", "content": self.prompt_builder.user_payload(input_data)},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": "call_result_classification",
                            "strict": True,
                            "schema": self.prompt_builder.schema(),
                        },
                    },
                    timeout=self.settings.llm_call_results_timeout_seconds,
                    store=False,
                )
                raw = response.choices[0].message.content or "{}"
                data = json.loads(raw)
                result = CallResultLLMResult.model_validate(data)
                usage = None
                if response.usage:
                    usage = {
                        "prompt": response.usage.prompt_tokens or 0,
                        "completion": response.usage.completion_tokens or 0,
                        "total": response.usage.total_tokens or 0,
                    }
                duration = int((time.monotonic() - start) * 1000)
                return ClassifyOutcome(
                    result=result,
                    duration_ms=duration,
                    token_usage=usage,
                    provider="openai",
                    model=self.model,
                )
            except (APITimeoutError, RateLimitError, APIConnectionError) as exc:
                last_error = type(exc).__name__
                if attempt < max_retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                duration = int((time.monotonic() - start) * 1000)
                return ClassifyOutcome(
                    result=None,
                    error_type=last_error.lower().replace("error", "timeout") if "timeout" in str(exc).lower() else "rate_limit",
                    error_message=str(exc),
                    duration_ms=duration,
                    provider="openai",
                    model=self.model,
                )
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = "schema"
                if attempt < max_retries:
                    continue
                duration = int((time.monotonic() - start) * 1000)
                return ClassifyOutcome(
                    result=None,
                    error_type="schema",
                    error_message=str(exc),
                    duration_ms=duration,
                    provider="openai",
                    model=self.model,
                )
            except Exception as exc:
                duration = int((time.monotonic() - start) * 1000)
                return ClassifyOutcome(
                    result=None,
                    error_type="error",
                    error_message=str(exc),
                    duration_ms=duration,
                    provider="openai",
                    model=self.model,
                )

        duration = int((time.monotonic() - start) * 1000)
        return ClassifyOutcome(
            result=None,
            error_type=last_error or "error",
            duration_ms=duration,
            provider="openai",
            model=self.model,
        )


def get_schema_version() -> str:
    return SCHEMA_VERSION
