"""Entra ID tokens for Azure OpenAI (ADR-0014): the gateway calls with its own identity.

``DefaultAzureCredential`` resolves, in order, environment service-principal variables, a
workload or managed identity, and the developer's ``az login`` session, so the same setting
works on a laptop and in Azure Container Apps. Needs the ``[azure]`` extra. The synchronous
credential is used from a worker thread: a token is fetched about once an hour, and the sync
client needs no extra HTTP stack.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
_REFRESH_MARGIN_SECONDS = 300


class TokenProvider(Protocol):
    async def token(self) -> str: ...

    async def aclose(self) -> None: ...


class EntraTokenProvider:
    """Bearer tokens for the Cognitive Services scope, cached until shortly before expiry."""

    def __init__(
        self, credential: Any | None = None, scope: str = COGNITIVE_SERVICES_SCOPE
    ) -> None:
        if credential is None:
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise RuntimeError(
                    "LFG_AZURE_OPENAI_AUTH=entra needs the azure extra: "
                    "pip install 'llm-finops-gateway[azure]'"
                ) from exc
            credential = DefaultAzureCredential()
        self._credential = credential
        self._scope = scope
        self._token: str | None = None
        self._expires_on: float = 0.0

    async def token(self) -> str:
        if self._token is None or time.time() >= self._expires_on - _REFRESH_MARGIN_SECONDS:
            access = await asyncio.to_thread(self._credential.get_token, self._scope)
            self._token = str(access.token)
            self._expires_on = float(access.expires_on)
        return self._token

    async def aclose(self) -> None:
        close = getattr(self._credential, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
