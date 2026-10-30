from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import CompletionResult, ProviderError, StreamChunk, iter_openai_sse, openai_usage


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

    @staticmethod
    def _payload(
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        max_completion_tokens: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"messages": messages}
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
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        payload = self._payload(messages, temperature, max_tokens, max_completion_tokens)
        try:
            resp = await self._client.post(
                self._url(model), params={"api-version": self._api_version}, json=payload
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
            raw=data,
        )

    async def stream(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        payload = self._payload(messages, temperature, max_tokens, max_completion_tokens)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        try:
            async with self._client.stream(
                "POST", self._url(model), params={"api-version": self._api_version}, json=payload
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
