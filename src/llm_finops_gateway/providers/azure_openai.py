from __future__ import annotations

from typing import Any

import httpx

from .base import CompletionResult, ProviderError


class AzureOpenAIProvider:
    """Azure OpenAI Service via the data-plane REST API.

    ``POST {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...``
    The model name in the request is mapped to a deployment name; unmapped names are used as-is.
    """

    name = "azure_openai"

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str = "2024-10-21",
        deployments: dict[str, str] | None = None,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_version = api_version
        self._deployments = dict(deployments or {})
        self._client = client or httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            timeout=timeout,
            headers={"api-key": api_key},
        )

    def deployment_for(self, model: str) -> str:
        return self._deployments.get(model, model)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        payload: dict[str, Any] = {"messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        url = f"/openai/deployments/{self.deployment_for(model)}/chat/completions"
        try:
            resp = await self._client.post(
                url, params={"api-version": self._api_version}, json=payload
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(self.name, resp.text[:200], status_code=resp.status_code)
        data: dict[str, Any] = resp.json()
        choice = data["choices"][0]
        usage: dict[str, Any] = data.get("usage") or {}
        return CompletionResult(
            content=str(choice["message"].get("content") or ""),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
