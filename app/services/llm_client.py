"""OpenAI API client factory."""

from __future__ import annotations

from openai import OpenAI

from app.config import Settings


def make_openai_client(settings: Settings, *, timeout: float | None = None) -> OpenAI | None:
    if not settings.openai_api_key:
        return None
    kwargs: dict[str, object] = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url.rstrip("/")
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)
