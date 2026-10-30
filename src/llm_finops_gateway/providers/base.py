from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class CompletionResult:
    content: str
    input_tokens: int
    output_tokens: int
    finish_reason: str = "stop"
    cached_input_tokens: int = 0  # subset of input_tokens served from the provider's prompt cache
    reasoning_tokens: int = (
        0  # subset of output_tokens spent on hidden reasoning (billed as output)
    )
    tool_calls: list[dict[str, Any]] | None = None  # OpenAI shape: id, type, function
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True)
class StreamChunk:
    """One piece of a streamed completion. ``usage`` is set on the final chunk when the
    provider reports it (ADR-0012)."""

    delta: str = ""
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    tool_calls: list[dict[str, Any]] | None = None  # OpenAI delta.tool_calls fragments


def openai_usage(usage: dict[str, Any]) -> TokenUsage:
    """Token counts from an OpenAI-shaped ``usage`` object (Azure OpenAI, OpenAI, vLLM...)."""
    prompt_details: dict[str, Any] = usage.get("prompt_tokens_details") or {}
    completion_details: dict[str, Any] = usage.get("completion_tokens_details") or {}
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens", 0)),
        output_tokens=int(usage.get("completion_tokens", 0)),
        cached_input_tokens=int(prompt_details.get("cached_tokens", 0)),
        reasoning_tokens=int(completion_details.get("reasoning_tokens", 0)),
    )


def openai_stream_chunk(data: dict[str, Any]) -> StreamChunk:
    """Map one ``chat.completion.chunk`` object. Chunks without choices carry usage or
    (on Azure) prompt filter results."""
    choices = data.get("choices") or []
    delta, finish, tool_calls = "", None, None
    if choices:
        part: dict[str, Any] = choices[0].get("delta") or {}
        delta = str(part.get("content") or "")
        tool_calls = part.get("tool_calls") or None
        finish = choices[0].get("finish_reason") or None
    usage = data.get("usage")
    return StreamChunk(
        delta=delta,
        finish_reason=finish,
        usage=openai_usage(usage) if usage else None,
        tool_calls=tool_calls,
    )


async def iter_openai_sse(response: httpx.Response) -> AsyncIterator[StreamChunk]:
    """Chunks from an OpenAI-style server-sent event stream, up to ``data: [DONE]``."""
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        yield openai_stream_chunk(json.loads(payload))


class ProviderError(RuntimeError):
    """Upstream call failed. ``status_code`` is the upstream HTTP status when known;
    ``retry_after`` is the upstream ``Retry-After`` header, if any."""

    def __init__(
        self,
        provider: str,
        message: str,
        status_code: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after


class Provider(Protocol):
    name: str

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> CompletionResult: ...

    def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]: ...

    async def aclose(self) -> None: ...
