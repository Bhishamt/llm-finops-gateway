from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.main import build_provider, build_providers
from llm_finops_gateway.providers.azure_openai import AzureOpenAIProvider
from llm_finops_gateway.providers.ollama import OllamaProvider
from llm_finops_gateway.providers.openai_compat import OpenAICompatProvider


def test_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LFG_PROVIDER", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.provider is ProviderKind.OLLAMA
    assert isinstance(build_provider(settings), OllamaProvider)


def test_azure_requires_endpoint_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # importing litellm loads the repository's .env into the process; keep this test clean
    for name in ("LFG_AZURE_OPENAI_ENDPOINT", "LFG_AZURE_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
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


def test_openai_compat_requires_base_url() -> None:
    with pytest.raises(ValueError) as exc:
        Settings(provider=ProviderKind.OPENAI_COMPAT, _env_file=None)  # type: ignore[call-arg]
    assert "LFG_OPENAI_COMPAT_BASE_URL" in str(exc.value)


def test_openai_compat_settings_build_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LFG_PROVIDER", "openai_compat")
    monkeypatch.setenv("LFG_OPENAI_COMPAT_BASE_URL", "https://res.openai.azure.com/openai/v1")
    monkeypatch.setenv("LFG_OPENAI_COMPAT_API_KEY", "s3cr3t-value")
    monkeypatch.setenv("LFG_OPENAI_COMPAT_HEADERS", '{"X-Tenant": "a"}')
    monkeypatch.setenv("LFG_OPENAI_COMPAT_MODELS", '{"gpt-4o-mini": "gpt-4o-mini-eastus"}')
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    provider = build_provider(settings)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.upstream_model_for("gpt-4o-mini") == "gpt-4o-mini-eastus"
    assert provider.upstream_model_for("other") == "other"
    assert settings.openai_compat_headers == {"X-Tenant": "a"}


def test_build_providers_adds_azure_next_to_default_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LFG_PROVIDER", raising=False)
    monkeypatch.setenv("LFG_AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("LFG_AZURE_OPENAI_API_KEY", "s3cr3t-value")
    monkeypatch.delenv("LFG_OPENAI_COMPAT_BASE_URL", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    providers = build_providers(settings)
    assert sorted(providers) == ["azure_openai", "ollama"]
    assert settings.provider is ProviderKind.OLLAMA
    assert isinstance(providers["azure_openai"], AzureOpenAIProvider)


def test_azure_endpoint_without_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LFG_PROVIDER", raising=False)
    monkeypatch.setenv("LFG_AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.delenv("LFG_AZURE_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="must be set together"):
        build_providers(Settings(_env_file=None))  # type: ignore[call-arg]


def test_azure_entra_auth_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("azure.identity", reason="[azure] extra not installed")
    monkeypatch.setenv("LFG_PROVIDER", "azure_openai")
    monkeypatch.setenv("LFG_AZURE_OPENAI_ENDPOINT", "https://res.openai.azure.com")
    monkeypatch.setenv("LFG_AZURE_OPENAI_AUTH", "entra")
    monkeypatch.delenv("LFG_AZURE_OPENAI_API_KEY", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.azure_openai_auth == "entra"
    providers = build_providers(settings)
    assert isinstance(providers["azure_openai"], AzureOpenAIProvider)
