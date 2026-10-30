from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .base import CompletionResult, ProviderError, StreamChunk, TokenUsage

log = logging.getLogger("llm_finops_gateway.providers.ollama")


def _to_ollama_message(message: dict[str, Any]) -> dict[str, Any]:
    """OpenAI message -> Ollama message: tool-call arguments become objects."""
    calls = message.get("tool_calls")
    if not calls:
        return message
    out = dict(message)
    out["tool_calls"] = [
        {
            **call,
            "function": {
                **call.get("function", {}),
                "arguments": _loads_or_keep(call.get("function", {}).get("arguments")),
            },
        }
        for call in calls
    ]
    return out


def _loads_or_keep(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _from_ollama_tool_calls(calls: Any) -> list[dict[str, Any]] | None:
    """Ollama tool calls -> OpenAI shape (string arguments, id, type)."""
    if not calls:
        return None
    out: list[dict[str, Any]] = []
    for i, call in enumerate(calls):
        fn = call.get("function") or {}
        args = fn.get("arguments")
        out.append(
            {
                "id": call.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "arguments": args if isinstance(args, str) else json.dumps(args or {}),
                },
            }
        )
    return out


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
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        max_completion_tokens: int | None,
        stream: bool,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ollama's /api/chat takes a subset of the OpenAI fields (ADR-0015): tools, a
        JSON output format, and sampling options. Tool-call arguments are objects on the
        Ollama side and JSON strings on the OpenAI side, so they are translated both ways."""
        extra = dict(extra or {})
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        limit = max_tokens if max_tokens is not None else max_completion_tokens
        if limit is not None:
            options["num_predict"] = limit
        for key in ("top_p", "seed"):
            if key in extra:
                options[key] = extra.pop(key)
        if "stop" in extra:
            stop = extra.pop("stop")
            options["stop"] = [stop] if isinstance(stop, str) else stop
        payload: dict[str, Any] = {
            "model": model,
            "messages": [_to_ollama_message(m) for m in messages],
            "stream": stream,
        }
        if options:
            payload["options"] = options
        if "tools" in extra:
            payload["tools"] = extra.pop("tools")
        fmt = extra.pop("response_format", None)
        if isinstance(fmt, dict):
            if fmt.get("type") == "json_object":
                payload["format"] = "json"
            elif fmt.get("type") == "json_schema":
                payload["format"] = (fmt.get("json_schema") or {}).get("schema", "json")
        extra.pop("tool_choice", None)  # Ollama decides itself
        if extra:
            log.debug("ollama: dropping unsupported fields %s", sorted(extra))
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
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult:
        payload = self._payload(
            model,
            messages,
            temperature,
            max_tokens,
            max_completion_tokens,
            stream=False,
            extra=extra,
        )
        try:
            resp = await self._client.post("/api/chat", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise self._error(resp, resp.text)
        data: dict[str, Any] = resp.json()
        tool_calls = _from_ollama_tool_calls(data["message"].get("tool_calls"))
        return CompletionResult(
            content=str(data["message"].get("content") or ""),
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            finish_reason="tool_calls" if tool_calls else str(data.get("done_reason", "stop")),
            tool_calls=tool_calls,
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
        """Ollama streams newline-delimited JSON; the ``done`` line carries the token counts."""
        payload = self._payload(
            model,
            messages,
            temperature,
            max_tokens,
            max_completion_tokens,
            stream=True,
            extra=extra,
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
                    message: dict[str, Any] = data.get("message") or {}
                    delta = str(message.get("content") or "")
                    calls = _from_ollama_tool_calls(message.get("tool_calls"))
                    if calls:
                        for i, call in enumerate(calls):
                            call["index"] = i
                    if not data.get("done"):
                        yield StreamChunk(delta=delta, tool_calls=calls)
                        continue
                    yield StreamChunk(
                        delta=delta,
                        tool_calls=calls,
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
