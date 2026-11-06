from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from llm_finops_gateway.attribution import Attribution
from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import CostEntry, Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import (
    PolicyEngine,
    PolicyError,
    PolicySet,
    _SlidingWindow,
    estimate_cost,
)
from llm_finops_gateway.pricing import PriceBook
from tests.conftest import FakeProvider

POLICIES_YAML = """\
version: test-pol
defaults:
  allowed_models: ["free"]
  budget: {monthly_usd: 10, warn_at: 0.8, block_at: 1.0, enforcement: block}
teams:
  platform:
    allowed_models: ["*"]
    budget: {monthly_usd: 1, warn_at: 0.5, block_at: 1.0}
  research:
    budget: {monthly_usd: 1, warn_at: 0.5, block_at: 1.0, enforcement: warn}
  nobudget:
    allowed_models: ["priced"]
  bursty:
    allowed_models: ["*"]
    rate_limit: {requests_per_minute: 2}
"""

EST = Decimal("0.01")  # a round estimate for engine-level tests


def _entry(team: str, cost: str, rid: str) -> CostEntry:
    return CostEntry(
        request_id=rid,
        team=team,
        project="p",
        use_case="u",
        provider="fake",
        model="priced",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal(cost),
        price_book_version="v",
    )


@pytest.fixture
def policy_set(tmp_path: Path) -> PolicySet:
    path = tmp_path / "policies.yaml"
    path.write_text(POLICIES_YAML)
    return PolicySet.from_yaml(path)


@pytest.fixture
def engine(policy_set: PolicySet, ledger: Ledger) -> PolicyEngine:
    return PolicyEngine(policy_set, ledger)


# ---- PolicySet resolution ---------------------------------------------------------------


def test_defaults_apply_to_unknown_team(policy_set: PolicySet) -> None:
    eff = policy_set.resolve("someone-else")
    assert eff.allowed_models == ["free"]
    assert eff.budget is not None and eff.budget.monthly_usd == Decimal("10")
    assert eff.rate_limit is None


def test_team_override_merges_with_defaults(policy_set: PolicySet) -> None:
    research = policy_set.resolve("research")
    assert research.allowed_models == ["free"]  # inherited
    assert research.budget is not None and research.budget.enforcement == "warn"
    nobudget = policy_set.resolve("nobudget")
    assert nobudget.allowed_models == ["priced"]
    assert nobudget.budget is not None and nobudget.budget.monthly_usd == Decimal("10")
    bursty = policy_set.resolve("bursty")
    assert bursty.rate_limit is not None and bursty.rate_limit.requests_per_minute == 2


def test_wildcard_allows_any_model(policy_set: PolicySet) -> None:
    assert policy_set.resolve("platform").allows_model("anything")


def test_empty_policy_set_allows_everything() -> None:
    eff = PolicySet.empty().resolve("x")
    assert eff.allows_model("whatever") and eff.budget is None and eff.rate_limit is None


@pytest.mark.parametrize(
    "content",
    [
        "defaults:\n  budget: {monthly_usd: 10, warn_at: 1.2, block_at: 1.0}\n",  # warn > block
        "defaults:\n  budget: {monthly_usd: 0}\n",  # non-positive
        "defaults:\n  budgett: {monthly_usd: 1}\n",  # typo, extra=forbid
        "defaults:\n  rate_limit: {requests_per_minute: 0}\n",
        "- a\n- list\n",
    ],
)
def test_malformed_policy_file_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(content)
    with pytest.raises(PolicyError):
        PolicySet.from_yaml(path)


# ---- estimate ---------------------------------------------------------------------------


def test_estimate_uses_chars_and_output_bound(price_book: PriceBook) -> None:
    msgs = [{"role": "user", "content": "x" * 400}]  # 100 tokens + 8 overhead
    # (108 * 2.5 + 1024 * 10) / 1e6
    assert estimate_cost(price_book, "priced", msgs, None, 1024) == Decimal("0.01051")
    # max_tokens caps the output part
    assert estimate_cost(price_book, "priced", msgs, 10, 1024) == Decimal("0.00037")
    assert estimate_cost(price_book, "free", msgs, None, 1024) == Decimal("0")


# ---- PolicyEngine decisions -------------------------------------------------------------


def test_model_not_allowed_is_blocked(engine: PolicyEngine) -> None:
    d = engine.check(Attribution("someone-else", "p"), "priced", EST)
    assert not d.allowed and d.violation == "model_not_allowed"
    assert "priced" in (d.message or "") and "free" in (d.message or "")


def test_budget_ok_reserves_estimate(engine: PolicyEngine, ledger: Ledger) -> None:
    ledger.record(_entry("platform", "0.40", "r1"))
    d = engine.check(Attribution("platform", "p"), "priced", EST)
    assert d.allowed and d.warnings == [] and d.reserved_usd == EST
    assert d.budget is not None and d.budget.level == "ok"
    assert d.budget.used_pct == Decimal("41.0")  # spent 0.40 + reserved 0.01
    assert ledger.counter("platform").reserved_usd == EST


def test_settling_replaces_reservation_with_actual_cost(
    engine: PolicyEngine, ledger: Ledger
) -> None:
    d = engine.check(Attribution("platform", "p"), "priced", EST)
    ledger.record(_entry("platform", "0.002", "r1"), release_reserved=d.reserved_usd)
    c = ledger.counter("platform")
    assert c.spent_usd == Decimal("0.002") and c.reserved_usd == Decimal("0")


def test_budget_warns_at_threshold(engine: PolicyEngine, ledger: Ledger) -> None:
    ledger.record(_entry("platform", "0.50", "r1"))
    d = engine.check(Attribution("platform", "p"), "priced", EST)
    assert d.allowed and d.reserved_usd == EST
    assert d.budget is not None and d.budget.level == "warn"
    assert len(d.warnings) == 1
    assert "51.0%" in d.warnings[0] and "blocked at 100%" in d.warnings[0]


def test_budget_blocks_when_estimate_does_not_fit(engine: PolicyEngine, ledger: Ledger) -> None:
    ledger.record(_entry("platform", "0.995", "r1"))  # 0.995 + 0.01 > 1.00
    d = engine.check(Attribution("platform", "p"), "priced", EST)
    assert not d.allowed and d.violation == "budget_exceeded"
    assert d.budget is not None and d.budget.level == "block"
    assert d.reserved_usd == Decimal(0)
    assert "estimated at 0.01" in (d.message or "")
    assert ledger.counter("platform").reserved_usd == Decimal(0)  # nothing left reserved


def test_budget_blocks_at_block_threshold(engine: PolicyEngine, ledger: Ledger) -> None:
    ledger.record(_entry("platform", "1.00", "r1"))
    d = engine.check(Attribution("platform", "p"), "priced", Decimal(0))
    assert not d.allowed and d.violation == "budget_exceeded"
    assert d.budget is not None and d.budget.remaining_usd == Decimal("0")


def test_warn_only_enforcement_never_blocks(engine: PolicyEngine, ledger: Ledger) -> None:
    ledger.record(_entry("research", "5.00", "r1"))  # 500 % of budget
    d = engine.check(Attribution("research", "p"), "free", EST)
    assert d.allowed and d.reserved_usd == EST
    assert d.budget is not None and d.budget.level == "warn"
    assert "warn-only" in d.warnings[0]


def test_budget_is_per_team(engine: PolicyEngine, ledger: Ledger) -> None:
    ledger.record(_entry("platform", "1.00", "r1"))
    assert engine.check(Attribution("nobudget", "p"), "priced", EST).allowed


def test_budget_status_none_without_budget() -> None:
    engine = PolicyEngine(PolicySet.empty(), Ledger("sqlite:///:memory:"))
    assert engine.budget_status("x") is None


def test_rate_limit_blocks_third_call_in_window(engine: PolicyEngine) -> None:
    attr = Attribution("bursty", "p")
    assert engine.check(attr, "priced", EST).allowed
    assert engine.check(attr, "priced", EST).allowed
    d = engine.check(attr, "priced", EST)
    assert not d.allowed and d.violation == "rate_limited"
    assert d.retry_after_seconds is not None and 1 <= d.retry_after_seconds <= 61


def test_sliding_window_frees_after_sixty_seconds() -> None:
    w = _SlidingWindow()
    assert w.hit("t", 1, now=0.0) is None
    assert w.hit("t", 1, now=30.0) == 31
    assert w.hit("t", 1, now=60.0) is None


# ---- HTTP integration -------------------------------------------------------------------


@pytest.fixture
def pclient(
    pricing_path: Path,
    price_book: PriceBook,
    policy_set: PolicySet,
    ledger: Ledger,
    fake_provider: FakeProvider,
) -> Iterator[TestClient]:
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=policy_set,
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        yield c


BODY = {"model": "priced", "messages": [{"role": "user", "content": "hi"}]}
HDR = {"X-Team": "platform", "X-Project": "demo"}
# estimate for BODY: (8 * 2.5 + 1024 * 10) / 1e6
BODY_ESTIMATE = Decimal("0.01026")


def test_http_model_not_allowed_403(pclient: TestClient, fake_provider: FakeProvider) -> None:
    r = pclient.post("/v1/chat/completions", json=BODY, headers={**HDR, "X-Team": "other"})
    assert r.status_code == 403
    d = r.json()["detail"]
    assert d["error"] == "model_not_allowed" and d["policy_version"] == "test-pol"
    assert fake_provider.calls == []


def test_http_budget_lifecycle_ok_warn_block(
    pclient: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    # platform: 1 USD budget, warn at 50 %, block at 100 %. Fake call costs 0.00075 USD.
    r = pclient.post("/v1/chat/completions", json=BODY, headers=HDR)
    assert r.status_code == 200
    assert r.headers["X-FinOps-Budget-Status"] == "ok"
    assert "X-FinOps-Warning" not in r.headers
    assert r.json()["x_finops"]["policy"]["warnings"] == []
    assert ledger.counter("platform").reserved_usd == Decimal(0)  # settled

    ledger.record(_entry("platform", "0.60", "seed-warn"))
    r = pclient.post("/v1/chat/completions", json=BODY, headers=HDR)
    assert r.status_code == 200
    assert r.headers["X-FinOps-Budget-Status"] == "warn"
    assert "monthly budget" in r.headers["X-FinOps-Warning"]
    body = r.json()["x_finops"]
    assert body["policy"]["budget"]["level"] == "warn"
    assert body["policy"]["warnings"][0].startswith("team 'platform' has used")
    assert "in flight" in body["policy"]["warnings"][0]
    # headers reflect the state *after* this call: spent includes it, nothing reserved
    assert Decimal(r.headers["X-FinOps-Budget-Spent-USD"]) == Decimal("0.6015")
    assert body["policy"]["budget"]["reserved_usd"] == "0"

    ledger.record(_entry("platform", "0.40", "seed-block"))
    r = pclient.post("/v1/chat/completions", json=BODY, headers=HDR)
    assert r.status_code == 403
    d = r.json()["detail"]
    assert d["error"] == "budget_exceeded"
    assert d["budget"]["level"] == "block"
    assert Decimal(d["budget"]["used_pct"]) >= 100
    assert str(BODY_ESTIMATE) in d["message"]
    assert len(fake_provider.calls) == 2  # blocked call never reached the provider


def test_http_warn_only_team_keeps_serving(pclient: TestClient, ledger: Ledger) -> None:
    ledger.record(_entry("research", "9.00", "seed"))
    r = pclient.post(
        "/v1/chat/completions",
        json={**BODY, "model": "free"},
        headers={**HDR, "X-Team": "research"},
    )
    assert r.status_code == 200
    assert r.headers["X-FinOps-Budget-Status"] == "warn"
    assert "warn-only" in r.headers["X-FinOps-Warning"]


def test_http_rate_limit_429_with_retry_after(pclient: TestClient) -> None:
    h = {**HDR, "X-Team": "bursty"}
    assert pclient.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
    assert pclient.post("/v1/chat/completions", json=BODY, headers=h).status_code == 200
    r = pclient.post("/v1/chat/completions", json=BODY, headers=h)
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1


def test_http_upstream_failure_releases_reservation(
    pclient: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    fake_provider.fail_with = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        pclient.post("/v1/chat/completions", json=BODY, headers=HDR)
    c = ledger.counter("platform")
    assert c.reserved_usd == Decimal(0) and c.spent_usd == Decimal(0)
    assert ledger.summary() == []


def test_http_budget_report(pclient: TestClient, ledger: Ledger) -> None:
    ledger.record(_entry("platform", "0.25", "r1"))
    ledger.record(_entry("adhoc-team", "0.25", "r2"))
    r = pclient.get("/v1/finops/budgets")
    assert r.status_code == 200
    body = r.json()
    assert body["policy_version"] == "test-pol"
    teams = {t["team"]: t for t in body["teams"]}
    assert set(teams) >= {"platform", "research", "nobudget", "adhoc-team", "bursty"}
    assert teams["platform"]["budget"]["used_pct"] == "25.0"
    assert teams["adhoc-team"]["budget"]["limit_usd"] == "10"  # defaults
    assert teams["platform"]["allowed_models"] == ["*"]
    assert teams["bursty"]["requests_per_minute"] == 2


def test_http_healthz_reports_policy_version(pclient: TestClient) -> None:
    body = pclient.get("/healthz").json()
    assert body["policies"] == "test-pol" and body["auth_mode"] == "headers"


def test_http_metrics_endpoint(pclient: TestClient) -> None:
    pclient.post("/v1/chat/completions", json=BODY, headers=HDR)
    text = pclient.get("/metrics").text
    assert 'lfg_requests_total{model="priced",outcome="ok",team="platform"} 1.0' in text
    assert "lfg_cost_usd_total" in text and "lfg_budget_used_ratio" in text


def test_estimate_counts_content_parts_tool_calls_and_tool_definitions() -> None:
    from llm_finops_gateway.policy import estimate_tokens

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": "x" * 40}, {"type": "image_url"}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "function": {}}]},
    ]
    plain, _ = estimate_tokens(messages, None, 10)
    with_tools, out = estimate_tokens(messages, None, 10, extra_chars=400)
    assert plain >= 40 // 4 + 16
    assert with_tools == plain + 100 and out == 10


# ---- project registry and project budgets (ADR-0021) ------------------------------------

REGISTRY_YAML = """\
version: registry
defaults:
  allowed_models: ["*"]
teams:
  product:
    budget: {monthly_usd: 1, warn_at: 0.5, block_at: 1.0}
    projects:
      chat-assistant:
        budget: {monthly_usd: 0.02, warn_at: 0.5, block_at: 1.0}
        use_cases: ["faq", "summaries"]
      internal-tools: {}
      narrow:
        allowed_models: ["free"]
  open:
    budget: {monthly_usd: 1}
"""


@pytest.fixture
def registry(tmp_path: Path, ledger: Ledger) -> PolicyEngine:
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY_YAML)
    return PolicyEngine(PolicySet.from_yaml(path), ledger)


def _attr(project: str, use_case: str = "faq", team: str = "product") -> Attribution:
    return Attribution(team=team, project=project, use_case=use_case)


def test_projects_under_defaults_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("defaults:\n  projects:\n    x: {}\n")
    with pytest.raises(PolicyError, match="per team"):
        PolicySet.from_yaml(path)


def test_unregistered_project_is_blocked_before_the_call(registry: PolicyEngine) -> None:
    d = registry.check(_attr("pilot-7"), "priced", EST)
    assert not d.allowed and d.violation == "project_not_registered"
    assert "chat-assistant, internal-tools, narrow" in (d.message or "")


def test_team_without_registry_accepts_any_project(registry: PolicyEngine) -> None:
    assert registry.check(_attr("anything", team="open"), "priced", EST).allowed


def test_registered_project_without_options_inherits_the_team(registry: PolicyEngine) -> None:
    d = registry.check(_attr("internal-tools", use_case="whatever"), "priced", EST)
    assert d.allowed and d.project_budget is None and d.budget is not None


def test_use_case_outside_the_list_is_blocked(registry: PolicyEngine) -> None:
    d = registry.check(_attr("chat-assistant", use_case="unspecified"), "priced", EST)
    assert not d.allowed and d.violation == "use_case_not_registered"
    assert "faq, summaries" in (d.message or "")


def test_project_model_list_narrows_the_team_list(registry: PolicyEngine) -> None:
    d = registry.check(_attr("narrow"), "priced", EST)
    assert not d.allowed and d.violation == "model_not_allowed"
    assert "project 'narrow'" in (d.message or "")
    assert registry.check(_attr("narrow"), "free", EST).allowed


def test_project_budget_is_reserved_and_settled_next_to_the_team(
    registry: PolicyEngine, ledger: Ledger
) -> None:
    d = registry.check(_attr("chat-assistant"), "priced", EST)
    assert d.allowed and d.project_budget is not None
    assert d.reserved_usd == EST and d.project_reserved_usd == EST
    assert ledger.counter("product").reserved_usd == EST
    assert ledger.counter("product/chat-assistant").reserved_usd == EST
    assert d.project_budget.used_pct == Decimal("50.0") and d.project_budget.level == "warn"
    assert d.warnings and d.warnings[0].startswith("project 'chat-assistant' of team 'product'")

    entry = CostEntry(
        request_id="r1",
        team="product",
        project="chat-assistant",
        use_case="faq",
        provider="fake",
        model="priced",
        input_tokens=1,
        output_tokens=1,
        cost_usd=Decimal("0.005"),
        price_book_version="v",
    )
    ledger.record(entry, release_reserved=d.reserved_usd, project_reserved=d.project_reserved_usd)
    team, proj = ledger.counter("product"), ledger.counter("product/chat-assistant")
    assert team.reserved_usd == 0 and team.spent_usd == Decimal("0.005")
    assert proj.reserved_usd == 0 and proj.spent_usd == Decimal("0.005")
    status = registry.project_budget_status("product", "chat-assistant")
    assert status is not None and status.used_pct == Decimal("25.0")
    assert registry.project_budget_status("product", "internal-tools") is None


def test_project_budget_blocks_and_releases_the_team_reservation(
    registry: PolicyEngine, ledger: Ledger
) -> None:
    ledger.reserve("product/chat-assistant", Decimal("0.015"), None)  # 75% in flight already
    d = registry.check(_attr("chat-assistant"), "priced", EST)  # EST 0.01 would exceed 0.02
    assert not d.allowed and d.violation == "budget_exceeded"
    assert d.project_budget is not None and d.project_budget.level == "block"
    assert d.budget is not None and d.budget.level == "ok"
    assert "project 'chat-assistant'" in (d.message or "")
    assert ledger.counter("product").reserved_usd == 0  # the team's reservation was given back


def test_http_project_budget_in_headers_and_body(
    tmp_path: Path,
    price_book: PriceBook,
    pricing_path: Path,
    ledger: Ledger,
    fake_provider: FakeProvider,
) -> None:
    path = tmp_path / "registry.yaml"
    path.write_text(REGISTRY_YAML)
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.from_yaml(path),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        hdr = {"X-Team": "product", "X-Project": "chat-assistant", "X-Use-Case": "faq"}
        r = c.post("/v1/chat/completions", json=BODY, headers=hdr)
        assert r.status_code == 200
        assert r.headers["X-FinOps-Project-Budget-Limit-USD"] == "0.02"
        assert r.headers["X-FinOps-Project-Budget-Status"] == "ok"
        body = r.json()["x_finops"]["policy"]
        assert body["project_budget"]["limit_usd"] == "0.02"
        assert body["budget"]["limit_usd"] == "1"
        assert ledger.counter("product/chat-assistant").reserved_usd == 0

        r = c.post("/v1/chat/completions", json=BODY, headers={**hdr, "X-Project": "pilot-7"})
        assert r.status_code == 403
        assert r.json()["detail"]["error"] == "project_not_registered"
        assert len(fake_provider.calls) == 1

        # the budgets report lists the registry with each project's effective policy
        teams = {t["team"]: t for t in c.get("/v1/finops/budgets").json()["teams"]}
        projects = {p["project"]: p for p in teams["product"]["projects"]}
        assert set(projects) == {"chat-assistant", "internal-tools", "narrow"}
        assert projects["chat-assistant"]["use_cases"] == ["faq", "summaries"]
        assert projects["chat-assistant"]["budget"]["limit_usd"] == "0.02"
        assert projects["chat-assistant"]["budget"]["spent_usd"] != "0"
        assert projects["internal-tools"]["budget"] is None
        assert projects["narrow"]["allowed_models"] == ["free"]
        assert projects["internal-tools"]["allowed_models"] == ["*"]
        assert teams["open"]["projects"] is None
