"""OpenAI API client factory."""

from __future__ import annotations

from openai import OpenAI

from app.config import Settings, normalize_llm_base_url


def make_openai_client(settings: Settings, *, timeout: float | None = None) -> OpenAI | None:
    if not settings.openai_api_key:
        return None
    kwargs: dict[str, object] = {"api_key": settings.openai_api_key}
    base_url = normalize_llm_base_url(settings.openai_base_url)
    if base_url:
        kwargs["base_url"] = base_url
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)
