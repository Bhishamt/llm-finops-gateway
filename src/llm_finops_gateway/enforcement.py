"""HTTP shape of a policy decision, shared by the FastAPI gateway and the LiteLLM plugin.

The decision itself is made by :class:`PolicyEngine`; this module only turns it into the
status code, error body and headers both front doors answer with, so that a client cannot
tell which one rejected the call (ADR-0019).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .attribution import Attribution
from .policy import BudgetStatus, PolicyDecision
from .pricing import money


def rejection(
    decision: PolicyDecision, attr: Attribution, model: str, policy_version: str
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Status code, error body and extra headers for a decision that was not allowed."""
    detail: dict[str, Any] = {
        "error": decision.violation,
        "message": decision.message,
        "team": attr.team,
        "project": attr.project,
        "model": model,
        "policy_version": policy_version,
    }
    headers: dict[str, str] = {}
    status_code = 403
    if decision.budget is not None:
        detail["budget"] = decision.budget.as_dict()
    if decision.project_budget is not None:
        detail["project_budget"] = decision.project_budget.as_dict()
    if decision.violation == "rate_limited":
        status_code = 429
        headers["Retry-After"] = str(decision.retry_after_seconds or 1)
    return status_code, detail, headers


def budget_headers(
    status: BudgetStatus | None,
    warnings: list[str],
    project_status: BudgetStatus | None = None,
) -> dict[str, str]:
    """The ``X-FinOps-Budget-*``, ``X-FinOps-Project-Budget-*`` and ``X-FinOps-Warning``
    headers after a call."""
    headers: dict[str, str] = {}
    if status is not None:
        headers["X-FinOps-Budget-Limit-USD"] = money(status.limit_usd)
        headers["X-FinOps-Budget-Spent-USD"] = money(status.spent_usd)
        headers["X-FinOps-Budget-Used-Pct"] = str(status.used_pct)
        headers["X-FinOps-Budget-Status"] = status.level
    if project_status is not None:
        headers["X-FinOps-Project-Budget-Limit-USD"] = money(project_status.limit_usd)
        headers["X-FinOps-Project-Budget-Spent-USD"] = money(project_status.spent_usd)
        headers["X-FinOps-Project-Budget-Used-Pct"] = str(project_status.used_pct)
        headers["X-FinOps-Project-Budget-Status"] = project_status.level
    if warnings:
        headers["X-FinOps-Warning"] = " | ".join(warnings)
    return headers


def cost_headers(request_id: str, cost: Decimal, currency: str) -> dict[str, str]:
    """The three headers every settled call carries."""
    return {
        "X-FinOps-Cost-USD": money(cost),
        "X-FinOps-Currency": currency,
        "X-FinOps-Request-Id": request_id,
    }
