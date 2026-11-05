from decimal import Decimal

from llm_finops_gateway.attribution import Attribution
from llm_finops_gateway.enforcement import budget_headers, cost_headers, rejection
from llm_finops_gateway.policy import BudgetLevel, BudgetStatus, PolicyDecision

ATTR = Attribution(team="platform", project="demo", use_case="unspecified", key_id="k1")


def _status(level: BudgetLevel = "warn") -> BudgetStatus:
    return BudgetStatus(
        limit_usd=Decimal("10"),
        spent_usd=Decimal("8.5"),
        reserved_usd=Decimal("0"),
        used_pct=Decimal("85.0"),
        remaining_usd=Decimal("1.5"),
        level=level,
        enforcement="block",
        warn_at_pct=Decimal("80"),
        block_at_pct=Decimal("100"),
    )


def test_budget_block_is_403_with_budget_in_body() -> None:
    decision = PolicyDecision(
        allowed=False, violation="budget_exceeded", message="over", budget=_status("block")
    )
    status, detail, headers = rejection(decision, ATTR, "gpt-4o-mini", "2026-11-01")
    assert status == 403
    assert detail["error"] == "budget_exceeded"
    assert detail["team"] == "platform" and detail["model"] == "gpt-4o-mini"
    assert detail["policy_version"] == "2026-11-01"
    assert detail["budget"]["level"] == "block"
    assert headers == {}


def test_rate_limit_is_429_with_retry_after() -> None:
    decision = PolicyDecision(
        allowed=False, violation="rate_limited", message="slow down", retry_after_seconds=7
    )
    status, detail, headers = rejection(decision, ATTR, "gpt-4o-mini", "v")
    assert status == 429
    assert headers == {"Retry-After": "7"}
    assert "budget" not in detail


def test_budget_headers_and_warning() -> None:
    h = budget_headers(_status(), ["budget at 85%"])
    assert h["X-FinOps-Budget-Limit-USD"] == "10"
    assert h["X-FinOps-Budget-Spent-USD"] == "8.5"
    assert h["X-FinOps-Budget-Used-Pct"] == "85.0"
    assert h["X-FinOps-Budget-Status"] == "warn"
    assert h["X-FinOps-Warning"] == "budget at 85%"
    assert budget_headers(None, []) == {}


def test_cost_headers_use_money_formatting() -> None:
    h = cost_headers("gw-1", Decimal("0.00001230"), "USD")
    assert h == {
        "X-FinOps-Cost-USD": "0.0000123",
        "X-FinOps-Currency": "USD",
        "X-FinOps-Request-Id": "gw-1",
    }
