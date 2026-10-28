"""Request and response schemas. The wire format mirrors the OpenAI Chat Completions API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["system", "user", "assistant", "tool"]


class ChatMessage(BaseModel):
    role: Role
    content: str


class ChatRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    stream: bool = False


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


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
