from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import CompletionResult, ProviderError, StreamChunk, TokenUsage


class OllamaProvider:
    """Local models via the Ollama HTTP API (``/api/chat``)."""

    name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    @staticmethod
    def _payload(
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        max_completion_tokens: int | None,
        stream: bool,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        limit = max_tokens if max_tokens is not None else max_completion_tokens
        if limit is not None:
            options["num_predict"] = limit
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
        if options:
            payload["options"] = options
        return payload

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
        payload = self._payload(
            model, messages, temperature, max_tokens, max_completion_tokens, stream=False
        )
        try:
            resp = await self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise self._error(resp, resp.text)
        data: dict[str, Any] = resp.json()
        return CompletionResult(
            content=str(data["message"]["content"]),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            finish_reason=str(data.get("done_reason", "stop")),
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
        """Ollama streams newline-delimited JSON; the ``done`` line carries the token counts."""
        payload = self._payload(
            model, messages, temperature, max_tokens, max_completion_tokens, stream=True
        )
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode(errors="replace")
                    raise self._error(resp, body)
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    data: dict[str, Any] = json.loads(line)
                    delta = str((data.get("message") or {}).get("content") or "")
                    if not data.get("done"):
                        yield StreamChunk(delta=delta)
                        continue
                    yield StreamChunk(
                        delta=delta,
                        finish_reason=str(data.get("done_reason") or "stop"),
                        usage=TokenUsage(
                            input_tokens=int(data.get("prompt_eval_count", 0)),
                            output_tokens=int(data.get("eval_count", 0)),
                        ),
                    )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()
