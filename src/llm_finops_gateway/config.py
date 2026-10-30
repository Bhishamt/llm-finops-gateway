"""Runtime configuration.

All settings come from environment variables prefixed with ``LFG_`` (or a ``.env`` file).
Secrets are never read from YAML; config files hold prices, policies and key *hashes* only.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderKind(StrEnum):
    OLLAMA = "ollama"
    AZURE_OPENAI = "azure_openai"
    OPENAI_COMPAT = "openai_compat"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LFG_", env_file=".env", extra="ignore")

    provider: ProviderKind = Field(
        default=ProviderKind.OLLAMA,
        description=(
            "Default provider for models whose price book row has no 'provider'. Every provider "
            "whose settings are present is available at the same time (ADR-0011)."
        ),
    )

    # Ollama
    ollama_base_url: str = "http://localhost:11434"

    # Azure OpenAI (required when provider == azure_openai)
    azure_openai_endpoint: str | None = Field(
        default=None, description="https://<resource>.openai.azure.com"
    )
    azure_openai_api_key: SecretStr | None = Field(
        default=None, description="Required with azure_openai_auth=api_key."
    )
    azure_openai_auth: Literal["api_key", "entra"] = Field(
        default="api_key",
        description=(
            "api_key: send the 'api-key' header. entra: bearer tokens from DefaultAzureCredential "
            "(managed identity, service principal or az login); needs the [azure] extra (ADR-0014)."
        ),
    )
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployments: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional map of model name -> deployment name. "
            "When a model is not listed, the model name is used as the deployment name."
        ),
    )

    # Any OpenAI-compatible server (required when provider == openai_compat, ADR-0010)
    openai_compat_base_url: str | None = Field(
        default=None,
        description=(
            "Base URL up to and including the API prefix, e.g. "
            "https://<resource>.openai.azure.com/openai/v1 or http://vllm:8000/v1"
        ),
    )
    openai_compat_api_key: SecretStr | None = Field(
        default=None,
        description="Sent as 'Authorization: Bearer'. Omit for servers on a trusted network.",
    )
    openai_compat_headers: dict[str, str] = Field(
        default_factory=dict, description="Extra request headers (JSON map)."
    )
    openai_compat_models: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional map of model name -> upstream model or deployment name. "
            "When a model is not listed, the model name is sent as-is."
        ),
    )

    # Who is calling (ADR-0006)
    auth_mode: Literal["api_key", "headers"] = Field(
        default="api_key",
        description=(
            "api_key: team comes from a team-bound key in Authorization: Bearer. "
            "headers: team is declared in X-Team (development / trusted network only)."
        ),
    )
    keys_path: Path = Path("config/keys.yaml")

    # Config files, storage
    pricing_path: Path = Path("config/pricing.yaml")
    policies_path: Path = Path("config/policies.yaml")
    database_url: str = "sqlite:///./gateway.db"
    request_timeout_seconds: float = 120.0

    # Alerts (ADR-0017): one JSON POST per budget or upstream event, once per cooldown
    alert_webhook_url: SecretStr | None = Field(
        default=None,
        description="Slack/Teams incoming webhook or any JSON endpoint; empty disables alerts.",
    )
    alert_cooldown_seconds: int = Field(
        default=3600, ge=0, description="Same event for the same team/provider at most this often."
    )

    # Observability
    metrics_enabled: bool = True
    otel_enabled: bool = Field(
        default=False,
        description=(
            "Emit OpenTelemetry spans. Exporter is configured with the standard OTEL_* "
            "variables (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_HEADERS). "
            "Requires the [otel] extra."
        ),
    )
    otel_service_name: str = "llm-finops-gateway"

    @model_validator(mode="after")
    def _azure_requires_credentials(self) -> Settings:
        if self.provider is ProviderKind.AZURE_OPENAI:
            required: list[tuple[str, object]] = [
                ("LFG_AZURE_OPENAI_ENDPOINT", self.azure_openai_endpoint)
            ]
            if self.azure_openai_auth == "api_key":
                required.append(("LFG_AZURE_OPENAI_API_KEY", self.azure_openai_api_key))
            missing = [name for name, value in required if value is None]
            if missing:
                raise ValueError(f"provider=azure_openai requires {missing}")
        return self

    @model_validator(mode="after")
    def _openai_compat_requires_base_url(self) -> Settings:
        if self.provider is ProviderKind.OPENAI_COMPAT and self.openai_compat_base_url is None:
            raise ValueError("provider=openai_compat requires ['LFG_OPENAI_COMPAT_BASE_URL']")
        return self
