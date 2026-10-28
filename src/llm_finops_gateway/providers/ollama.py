from __future__ import annotations

from typing import Any

import httpx

from .base import CompletionResult, ProviderError


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

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if options:
            payload["options"] = options
        try:
            resp = await self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(self.name, resp.text[:200], status_code=resp.status_code)
        data: dict[str, Any] = resp.json()
        return CompletionResult(
            content=str(data["message"]["content"]),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            finish_reason=str(data.get("done_reason", "stop")),
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
