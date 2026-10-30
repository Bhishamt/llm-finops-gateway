from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from .azure_identity import TokenProvider
from .base import CompletionResult, ProviderError, StreamChunk, iter_openai_sse, openai_usage


class AzureOpenAIProvider:
    """Azure OpenAI Service via the data-plane REST API.

    ``POST {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...``
    The model name in the request is mapped to a deployment name; unmapped names are used as-is.
    Authenticates with an API key (``api-key`` header) or, given a ``token_provider``, with an
    Entra ID bearer token fetched per request (ADR-0014).
    """

    name = "azure_openai"

    def __init__(
        self,
        endpoint: str,
        api_key: str | None = None,
        api_version: str = "2024-10-21",
        deployments: dict[str, str] | None = None,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
        token_provider: TokenProvider | None = None,
    ) -> None:
        if (api_key is None) == (token_provider is None):
            raise ValueError("AzureOpenAIProvider needs exactly one of api_key or token_provider")
        self._api_version = api_version
        self._deployments = dict(deployments or {})
        self._token_provider = token_provider
        self._client = client or httpx.AsyncClient(
            base_url=endpoint.rstrip("/"),
            timeout=timeout,
            headers={"api-key": api_key} if api_key is not None else {},
        )

    async def _auth_headers(self) -> dict[str, str]:
        if self._token_provider is None:
            return {}
        return {"Authorization": f"Bearer {await self._token_provider.token()}"}

    def deployment_for(self, model: str) -> str:
        return self._deployments.get(model, model)

    @staticmethod
    def _payload(
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        max_completion_tokens: int | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": messages, **(extra or {})}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if max_completion_tokens is not None:
            payload["max_completion_tokens"] = max_completion_tokens
        return payload

    def _url(self, model: str) -> str:
        return f"/openai/deployments/{self.deployment_for(model)}/chat/completions"

    def _error(self, resp: httpx.Response, body: str) -> ProviderError:
        return ProviderError(
            self.name,
            body[:200],
            status_code=resp.status_code,
            retry_after=resp.headers.get("Retry-After"),
        )

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult:
        payload = self._payload(messages, temperature, max_tokens, max_completion_tokens, extra)
        try:
            resp = await self._client.post(
                self._url(model),
                params={"api-version": self._api_version},
                json=payload,
                headers=await self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise self._error(resp, resp.text)
        data: dict[str, Any] = resp.json()
        choice = data["choices"][0]
        usage = openai_usage(data.get("usage") or {})
        return CompletionResult(
            content=str(choice["message"].get("content") or ""),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            finish_reason=str(choice.get("finish_reason") or "stop"),
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            tool_calls=choice["message"].get("tool_calls") or None,
            raw=data,
        )

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload = self._payload(messages, temperature, max_tokens, max_completion_tokens, extra)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        try:
            async with self._client.stream(
                "POST",
                self._url(model),
                params={"api-version": self._api_version},
                json=payload,
                headers=await self._auth_headers(),
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise self._error(resp, body)
                async for chunk in iter_openai_sse(resp):
                    yield chunk
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._token_provider is not None:
            await self._token_provider.aclose()
