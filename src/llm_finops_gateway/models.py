"""Request and response schemas. The wire format mirrors the OpenAI Chat Completions API."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
)

Role = Literal["system", "developer", "user", "assistant", "tool"]


class FunctionCall(BaseModel):
    name: str
    arguments: str


class ToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    """One message in the OpenAI shape. ``content`` is text, a list of content parts (text,
    image_url, ...) or null for assistant messages that only carry ``tool_calls``."""

    role: Role
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_optionals(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("name", "tool_calls", "tool_call_id"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ChatRequest(BaseModel):
    """The accepted OpenAI Chat Completions surface (ADR-0015). Unknown fields are rejected
    with 422 rather than dropped; ``PASSTHROUGH`` fields reach the provider unchanged."""

    model_config = ConfigDict(extra="forbid")

    PASSTHROUGH: ClassVar[tuple[str, ...]] = (
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "response_format",
        "top_p",
        "stop",
        "seed",
        "user",
        "presence_penalty",
        "frequency_penalty",
        "logprobs",
        "top_logprobs",
    )

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False
    stream_options: dict[str, Any] | None = None  # accepted; usage is always included
    n: int | None = Field(default=None, ge=1, le=1)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: str | list[str] | None = None
    seed: int | None = None
    user: str | None = None
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    logprobs: bool | None = None
    top_logprobs: int | None = Field(default=None, ge=0, le=20)

    @property
    def output_limit(self) -> int | None:
        """The client's output bound, whichever spelling it used."""
        return self.max_tokens if self.max_tokens is not None else self.max_completion_tokens

    def passthrough(self) -> dict[str, Any]:
        return {k: v for k in self.PASSTHROUGH if (v := getattr(self, k)) is not None}


class PromptTokensDetails(BaseModel):
    cached_tokens: int


class CompletionTokensDetails(BaseModel):
    reasoning_tokens: int


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: PromptTokensDetails | None = None
    completion_tokens_details: CompletionTokensDetails | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_details(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        for key in ("prompt_tokens_details", "completion_tokens_details"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class Choice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"


class BudgetEnvelope(BaseModel):
    limit_usd: str
    spent_usd: str
    reserved_usd: str
    used_pct: str
    remaining_usd: str
    level: Literal["ok", "warn", "block"]
    enforcement: Literal["block", "warn"]
    warn_at_pct: str
    block_at_pct: str


class PolicyEnvelope(BaseModel):
    version: str
    budget: BudgetEnvelope | None = None
    warnings: list[str] = Field(default_factory=list)


class FinOpsEnvelope(BaseModel):
    """Gateway-specific extension carried alongside the OpenAI-compatible response."""

    cost_usd: str
    price_book_version: str
    team: str
    project: str
    use_case: str
    provider: str
    key_id: str | None = None
    policy: PolicyEnvelope


class TeamBudgetRow(BaseModel):
    team: str
    allowed_models: list[str]
    requests_per_minute: int | None = None
    budget: BudgetEnvelope | None


class BudgetReport(BaseModel):
    policy_version: str
    month_start: str
    teams: list[TeamBudgetRow]


class ChatResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage
    x_finops: FinOpsEnvelope


class CostSummaryRow(BaseModel):
    team: str
    project: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: str


class CostSummary(BaseModel):
    currency: str
    rows: list[CostSummaryRow]
    total_cost_usd: str
