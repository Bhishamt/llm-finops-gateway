from __future__ import annotations

from typing import Any

import httpx

from .base import CompletionResult, ProviderError


class OpenAICompatProvider:
    """Any server that speaks the OpenAI Chat Completions API.

    ``POST {base_url}/chat/completions`` with ``Authorization: Bearer <api_key>``.
    Covers OpenAI, the Azure OpenAI v1 endpoint (``https://<resource>.openai.azure.com/openai/v1``,
    where the model is the deployment name), vLLM, LocalAI, TGI and Ollama's ``/v1``.
    The model name in the request can be mapped to the upstream name; unmapped names are used as-is.
    """

    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        models: dict[str, str] | None = None,
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._models = dict(models or {})
        default_headers: dict[str, str] = {}
        if api_key:
            default_headers["Authorization"] = f"Bearer {api_key}"
        default_headers.update(headers or {})
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout, headers=default_headers
        )

    def upstream_model_for(self, model: str) -> str:
        return self._models.get(model, model)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.upstream_model_for(model),
            "messages": messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if max_completion_tokens is not None:
            payload["max_completion_tokens"] = max_completion_tokens
        try:
            resp = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                self.name,
                resp.text[:200],
                status_code=resp.status_code,
                retry_after=resp.headers.get("Retry-After"),
            )
        data: dict[str, Any] = resp.json()
        choice = data["choices"][0]
        usage: dict[str, Any] = data.get("usage") or {}
        prompt_details: dict[str, Any] = usage.get("prompt_tokens_details") or {}
        completion_details: dict[str, Any] = usage.get("completion_tokens_details") or {}
        return CompletionResult(
            content=str(choice["message"].get("content") or ""),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            cached_input_tokens=int(prompt_details.get("cached_tokens", 0)),
            reasoning_tokens=int(completion_details.get("reasoning_tokens", 0)),
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
