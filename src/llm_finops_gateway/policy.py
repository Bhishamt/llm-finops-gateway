"""Policy as code.

Policies are declared in ``config/policies.yaml`` and evaluated per request, keyed on the
attribution dimensions. Supported policy types:

* **model allow-list** per team (``allowed_models``), ``"*"`` meaning any priced model;
* **monthly budget** per team with two thresholds: ``warn_at`` (default 80 %) adds a warning to
  the response and the log, ``block_at`` (default 100 %) rejects the call with HTTP 403.
  ``enforcement: warn`` turns blocking off for a team. The budget is enforced with an atomic
  reservation of the call's estimated cost before the upstream call (ADR-0007), so two
  concurrent calls cannot both slip past the limit;
* **rate limit** per team (``rate_limit.requests_per_minute``), HTTP 429 with ``Retry-After``.
  Sliding window, in memory, per gateway instance.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .attribution import Attribution
from .ledger import Ledger
from .pricing import PriceBook, money

log = logging.getLogger("llm_finops_gateway.policy")

BudgetLevel = Literal["ok", "warn", "block"]
Enforcement = Literal["block", "warn"]


class PolicyError(ValueError):
    """Raised when the policy file is malformed."""


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    monthly_usd: Decimal = Field(gt=0)
    warn_at: float = Field(default=0.8, ge=0, le=10)
    block_at: float = Field(default=1.0, ge=0, le=10)
    enforcement: Enforcement = "block"
    reserve_output_tokens: int = Field(
        default=1024,
        ge=1,
        description="Output tokens assumed for the pre-call estimate when max_tokens is absent",
    )

    @model_validator(mode="after")
    def _warn_before_block(self) -> BudgetPolicy:
        if self.warn_at > self.block_at:
            raise ValueError("warn_at must be <= block_at")
        return self


class RateLimitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(gt=0)


class TeamPolicy(BaseModel):
    """Partial policy; ``None`` means 'inherit from defaults'."""

    model_config = ConfigDict(extra="forbid")

    allowed_models: list[str] | None = None
    budget: BudgetPolicy | None = None
    rate_limit: RateLimitPolicy | None = None


class EffectivePolicy(BaseModel):
    """Fully resolved policy for one team."""

    model_config = ConfigDict(frozen=True)

    team: str
    allowed_models: list[str]
    budget: BudgetPolicy | None
    rate_limit: RateLimitPolicy | None

    def allows_model(self, model: str) -> bool:
        return "*" in self.allowed_models or model in self.allowed_models


@dataclass(frozen=True)
class BudgetStatus:
    limit_usd: Decimal
    spent_usd: Decimal
    reserved_usd: Decimal
    used_pct: Decimal
    remaining_usd: Decimal
    level: BudgetLevel
    enforcement: Enforcement
    warn_at_pct: Decimal
    block_at_pct: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit_usd": money(self.limit_usd),
            "spent_usd": money(self.spent_usd),
            "reserved_usd": money(self.reserved_usd),
            "used_pct": str(self.used_pct),
            "remaining_usd": money(self.remaining_usd),
            "level": self.level,
            "enforcement": self.enforcement,
            "warn_at_pct": str(self.warn_at_pct),
            "block_at_pct": str(self.block_at_pct),
        }


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    violation: str | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)
    budget: BudgetStatus | None = None
    reserved_usd: Decimal = Decimal(0)
    retry_after_seconds: int | None = None


class PolicySet:
    def __init__(self, defaults: TeamPolicy, teams: dict[str, TeamPolicy], version: str) -> None:
        self._defaults = defaults
        self._teams = dict(teams)
        self.version = version

    @classmethod
    def empty(cls) -> PolicySet:
        """No policy file: everything priced is allowed, no budgets, no rate limits."""
        return cls(TeamPolicy(allowed_models=["*"]), {}, version="none")

    @classmethod
    def from_yaml(cls, path: Path) -> PolicySet:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise PolicyError(f"{path}: top level must be a mapping")
        try:
            defaults = TeamPolicy.model_validate(raw.get("defaults") or {})
            teams = {
                str(name): TeamPolicy.model_validate(spec or {})
                for name, spec in (raw.get("teams") or {}).items()
            }
        except ValueError as exc:
            raise PolicyError(f"{path}: {exc}") from exc
        return cls(defaults, teams, version=str(raw.get("version", "unversioned")))

    def teams(self) -> list[str]:
        return sorted(self._teams)

    def resolve(self, team: str) -> EffectivePolicy:
        override = self._teams.get(team, TeamPolicy())
        allowed = (
            override.allowed_models
            if override.allowed_models is not None
            else (self._defaults.allowed_models or ["*"])
        )
        budget = override.budget if override.budget is not None else self._defaults.budget
        rate = override.rate_limit if override.rate_limit is not None else self._defaults.rate_limit
        return EffectivePolicy(
            team=team, allowed_models=list(allowed), budget=budget, rate_limit=rate
        )


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _message_chars(message: dict[str, Any]) -> int:
    """Characters that reach the model: text content or text parts, plus tool-call payloads."""
    content = message.get("content")
    chars = 0
    if isinstance(content, str):
        chars += len(content)
    elif isinstance(content, list):
        chars += sum(len(str(p.get("text", ""))) for p in content if isinstance(p, dict))
    if message.get("tool_calls"):
        chars += len(json.dumps(message["tool_calls"]))
    return chars


def estimate_tokens(
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    reserve_output_tokens: int,
    extra_chars: int = 0,
) -> tuple[int, int]:
    """(input, output) token estimate without a tokenizer: prompt chars / 4 plus a per-message
    overhead and ``extra_chars`` (tool definitions); output bounded by max_tokens or the
    policy's default."""
    prompt_chars = sum(_message_chars(m) for m in messages) + extra_chars
    return prompt_chars // 4 + 8 * len(messages), max_tokens or reserve_output_tokens


def estimate_cost(
    book: PriceBook,
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int | None,
    reserve_output_tokens: int,
    extra_chars: int = 0,
) -> Decimal:
    """Upper-ish bound of a call's cost for budget reservation."""
    input_tokens, output_tokens = estimate_tokens(
        messages, max_tokens, reserve_output_tokens, extra_chars
    )
    return book.cost(model, input_tokens, output_tokens)


class _SlidingWindow:
    """Requests in the last 60 s per team; good enough for one instance."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, team: str, limit: int, now: float | None = None) -> int | None:
        """Record a request. Returns None when allowed, else seconds until the window frees."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            q = self._hits.setdefault(team, deque())
            while q and now - q[0] >= 60:
                q.popleft()
            if len(q) >= limit:
                return max(1, int(60 - (now - q[0])) + 1)
            q.append(now)
            return None


class PolicyEngine:
    def __init__(self, policies: PolicySet, ledger: Ledger) -> None:
        self._policies = policies
        self._ledger = ledger
        self._window = _SlidingWindow()

    @property
    def version(self) -> str:
        return self._policies.version

    def resolve(self, team: str) -> EffectivePolicy:
        return self._policies.resolve(team)

    def budget_status(self, team: str) -> BudgetStatus | None:
        policy = self._policies.resolve(team)
        if policy.budget is None:
            return None
        c = self._ledger.counter(team)
        return _status(policy.budget, c.spent_usd, c.reserved_usd)

    def check(self, attr: Attribution, model: str, estimate: Decimal) -> PolicyDecision:
        """Pre-call evaluation: allow-list, rate limit, then budget reservation.

        On success with a budget, ``reserved_usd`` has been added to the team's counter and
        must be settled with ``Ledger.record(..., release_reserved=...)`` or returned with
        ``Ledger.release()`` by the caller.
        """
        policy = self._policies.resolve(attr.team)

        if not policy.allows_model(model):
            msg = (
                f"model '{model}' is not allowed for team '{attr.team}' "
                f"(allowed: {', '.join(policy.allowed_models)})"
            )
            log.warning(
                "policy block team=%s project=%s model=%s reason=model_not_allowed",
                attr.team,
                attr.project,
                model,
            )
            return PolicyDecision(allowed=False, violation="model_not_allowed", message=msg)

        if policy.rate_limit is not None:
            retry = self._window.hit(attr.team, policy.rate_limit.requests_per_minute)
            if retry is not None:
                log.warning(
                    "policy block team=%s project=%s reason=rate_limited rpm=%s",
                    attr.team,
                    attr.project,
                    policy.rate_limit.requests_per_minute,
                )
                return PolicyDecision(
                    allowed=False,
                    violation="rate_limited",
                    message=(
                        f"team '{attr.team}' exceeded {policy.rate_limit.requests_per_minute} "
                        f"requests per minute"
                    ),
                    retry_after_seconds=retry,
                )

        budget = policy.budget
        if budget is None:
            return PolicyDecision(allowed=True)

        limit_for_block = (
            budget.monthly_usd * Decimal(str(budget.block_at))
            if budget.enforcement == "block"
            else None
        )
        before = self._ledger.counter(attr.team)
        exhausted = limit_for_block is not None and before.committed_usd >= limit_for_block
        reserved = (
            False if exhausted else self._ledger.reserve(attr.team, estimate, limit_for_block)
        )
        c = self._ledger.counter(attr.team)
        status = _status(budget, c.spent_usd, c.reserved_usd)
        budget_msg = (
            f"team '{attr.team}' has used {status.used_pct}% of its monthly budget "
            f"({money(status.spent_usd)} spent + {money(status.reserved_usd)} in flight of "
            f"{money(status.limit_usd)} USD)"
        )
        if not reserved:
            log.warning(
                "policy block team=%s project=%s reason=budget_exceeded used_pct=%s estimate=%s",
                attr.team,
                attr.project,
                status.used_pct,
                estimate,
            )
            blocked = _status(budget, c.spent_usd, c.reserved_usd, force_block=True)
            return PolicyDecision(
                allowed=False,
                violation="budget_exceeded",
                message=f"{budget_msg}; this call was estimated at {money(estimate)} USD",
                budget=blocked,
            )
        if status.level in ("warn", "block"):
            level_note = (
                f"calls will be blocked at {status.block_at_pct}%"
                if budget.enforcement == "block"
                else "enforcement is warn-only"
            )
            log.warning(
                "policy warn team=%s project=%s used_pct=%s",
                attr.team,
                attr.project,
                status.used_pct,
            )
            return PolicyDecision(
                allowed=True,
                warnings=[f"{budget_msg}; {level_note}"],
                budget=status,
                reserved_usd=estimate,
            )
        return PolicyDecision(allowed=True, budget=status, reserved_usd=estimate)


def _status(
    budget: BudgetPolicy, spent: Decimal, reserved: Decimal, *, force_block: bool = False
) -> BudgetStatus:
    limit = budget.monthly_usd
    committed = spent + reserved
    used_pct = (committed / limit * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    ratio = committed / limit
    level: BudgetLevel = "ok"
    if force_block or (ratio >= Decimal(str(budget.block_at)) and budget.enforcement == "block"):
        level = "block"
    elif ratio >= Decimal(str(budget.warn_at)):
        level = "warn"
    return BudgetStatus(
        limit_usd=limit,
        spent_usd=spent,
        reserved_usd=reserved,
        used_pct=used_pct,
        remaining_usd=max(limit - committed, Decimal(0)),
        level=level,
        enforcement=budget.enforcement,
        warn_at_pct=(Decimal(str(budget.warn_at)) * 100).quantize(Decimal("1")),
        block_at_pct=(Decimal(str(budget.block_at)) * 100).quantize(Decimal("1")),
    )
