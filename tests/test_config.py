from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.main import build_provider
from llm_finops_gateway.providers.azure_openai import AzureOpenAIProvider
from llm_finops_gateway.providers.ollama import OllamaProvider


def test_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LFG_PROVIDER", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.provider is ProviderKind.OLLAMA
    assert isinstance(build_provider(settings), OllamaProvider)


def test_azure_requires_endpoint_and_key() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(provider=ProviderKind.AZURE_OPENAI, _env_file=None)  # type: ignore[call-arg]
    assert "LFG_AZURE_OPENAI_ENDPOINT" in str(exc.value)


def test_azure_settings_build_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LFG_PROVIDER", "azure_openai")
    monkeypatch.setenv("LFG_AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("LFG_AZURE_OPENAI_API_KEY", "s3cr3t-value")
    monkeypatch.setenv("LFG_AZURE_OPENAI_DEPLOYMENTS", '{"gpt-4o": "gpt4o-prod"}')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = build_provider(settings)
    assert isinstance(provider, AzureOpenAIProvider)
    assert provider.deployment_for("gpt-4o") == "gpt4o-prod"
    assert provider.deployment_for("other") == "other"
    assert "s3cr3t-value" not in repr(settings)  # secret is masked
