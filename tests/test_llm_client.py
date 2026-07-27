"""Tests for shared LLM client factory."""

from __future__ import annotations

from unittest.mock import patch

from app.config import Settings, get_llm_provider_label, merge_db_settings
from app.services.llm_client import make_openai_client


def test_make_openai_client_returns_none_without_key():
    settings = Settings.model_construct(openai_api_key="")
    assert make_openai_client(settings) is None


def test_make_openai_client_passes_base_url_and_timeout():
    settings = Settings.model_construct(
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1/",
    )
    with patch("app.services.llm_client.OpenAI") as openai_cls:
        client = make_openai_client(settings, timeout=12.5)
    openai_cls.assert_called_once_with(
        api_key="sk-test",
        base_url="https://api.openai.com/v1",
        timeout=12.5,
    )
    assert client is openai_cls.return_value


def test_make_openai_client_omits_base_url_when_empty():
    settings = Settings.model_construct(
        openai_api_key="sk-test",
        openai_base_url="",
    )
    with patch("app.services.llm_client.OpenAI") as openai_cls:
        make_openai_client(settings)
    openai_cls.assert_called_once_with(api_key="sk-test")


def test_get_llm_provider_label():
    openai_url = Settings.model_construct(openai_base_url="https://api.openai.com/v1")
    vsellm = Settings.model_construct(openai_base_url="https://api.vsellm.ru/v1")
    custom = Settings.model_construct(openai_base_url="https://example.com/v1")
    direct = Settings.model_construct(openai_base_url="")
    assert get_llm_provider_label(openai_url) == "openai"
    assert get_llm_provider_label(vsellm) == "vsellm"
    assert get_llm_provider_label(custom) == "custom"
    assert get_llm_provider_label(direct) == "openai"


def test_merge_db_settings_preserves_vsellm_url_and_model_namespaces():
    settings = merge_db_settings(
        {
            "openai_base_url": "https://api.vsellm.ru",
            "openai_model": "openai/gpt-4o",
            "openai_bitrix_metadata_model": "openai/gpt-4o-mini",
        }
    )
    assert settings.openai_base_url == "https://api.vsellm.ru/v1"
    assert settings.openai_model == "openai/gpt-4o"
    assert settings.openai_bitrix_metadata_model == "openai/gpt-4o-mini"


def test_merge_db_settings_strips_gateway_namespace_for_direct_openai():
    settings = merge_db_settings(
        {
            "openai_base_url": "https://api.openai.com/v1",
            "openai_model": "openai/gpt-4o",
            "openai_bitrix_metadata_model": "openai/gpt-4o-mini",
        }
    )
    assert settings.openai_model == "gpt-4o"
    assert settings.openai_bitrix_metadata_model == "gpt-4o-mini"


def test_make_openai_client_normalizes_vsellm_base_url():
    settings = Settings.model_construct(
        openai_api_key="sk-vsellm-test",
        openai_base_url="https://api.vsellm.ru/",
    )
    with patch("app.services.llm_client.OpenAI") as openai_cls:
        make_openai_client(settings)
    openai_cls.assert_called_once_with(
        api_key="sk-vsellm-test",
        base_url="https://api.vsellm.ru/v1",
    )
