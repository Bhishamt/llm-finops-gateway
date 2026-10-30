from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


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
    raw: dict[str, Any] = field(default_factory=dict)


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
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_completion_tokens: int | None = None,
    ) -> CompletionResult: ...

    async def aclose(self) -> None: ...
