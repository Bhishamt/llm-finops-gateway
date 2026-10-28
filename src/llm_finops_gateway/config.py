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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LFG_", env_file=".env", extra="ignore")

    provider: ProviderKind = ProviderKind.OLLAMA

    # Ollama
    ollama_base_url: str = "http://localhost:11434"

    # Azure OpenAI (required when provider == azure_openai)
    azure_openai_endpoint: str | None = Field(
        default=None, description="https://<resource>.openai.azure.com"
    )
    azure_openai_api_key: SecretStr | None = None
    azure_openai_api_version: str = "2024-10-21"
    azure_openai_deployments: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Optional map of model name -> deployment name. "
            "When a model is not listed, the model name is used as the deployment name."
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
            missing = [
                name
                for name, value in (
                    ("LFG_AZURE_OPENAI_ENDPOINT", self.azure_openai_endpoint),
                    ("LFG_AZURE_OPENAI_API_KEY", self.azure_openai_api_key),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"provider=azure_openai requires {missing}")
        return self
