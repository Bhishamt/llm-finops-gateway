"""The LiteLLM plugin, exercised through its hooks without a LiteLLM server."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

pytest.importorskip("litellm", reason="[litellm] extra not installed")

from llm_finops_gateway.ledger import Ledger  # noqa: E402
from llm_finops_gateway.litellm_plugin import META, FinOpsPlugin  # noqa: E402
from llm_finops_gateway.policy import PolicySet  # noqa: E402
from llm_finops_gateway.pricing import PriceBook  # noqa: E402

PRICING = """\
version: "2026-11-05"
currency: USD
models:
  gpt-4o-mini: { input: 0.15, output: 0.60, cached_input: 0.075 }
"""
POLICIES = """\
version: "2026-11-05"
defaults:
  allowed_models: ["*"]
teams:
  customer-support:
    budget: { monthly_usd: 0.001, warn_at: 0.5, block_at: 1.0, reserve_output_tokens: 10 }
  research:
    budget:
      { monthly_usd: 0.001, warn_at: 0.5, block_at: 1.0, enforcement: warn,
        reserve_output_tokens: 10 }
"""


@pytest.fixture
def ledger() -> Ledger:
    return Ledger("sqlite:///:memory:")


@pytest.fixture
def plugin(tmp_path: Path, ledger: Ledger) -> FinOpsPlugin:
    (tmp_path / "pricing.yaml").write_text(PRICING)
    (tmp_path / "policies.yaml").write_text(POLICIES)
    return FinOpsPlugin(
        price_book=PriceBook.from_yaml(tmp_path / "pricing.yaml"),
        policies=PolicySet.from_yaml(tmp_path / "policies.yaml"),
        ledger=ledger,
    )


def detail_of(exc: HTTPException) -> dict[str, Any]:
    detail: Any = exc.detail  # starlette types it as str; the gateway raises dicts
    assert isinstance(detail, dict)
    return detail


def key(team: str | None = "customer-support", alias: str = "cs-app-1") -> SimpleNamespace:
    return SimpleNamespace(team_alias=team, team_id="t-1", key_alias=alias)


def request(
    model: str = "gpt-4o-mini", project: str | None = "chat-assistant", **extra: Any
) -> dict[str, Any]:
    headers = {"content-type": "application/json"}
    if project:
        headers["x-project"] = project
    data: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 5,
        "metadata": {"endpoint": "/chat/completions"},
        "proxy_server_request": {"headers": headers},
    }
    data.update(extra)
    return data


def response(prompt: int = 9, completion: int = 1, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
            completion_tokens_details=None,
        ),
        _hidden_params={"custom_llm_provider": "azure"},
    )


def success_kwargs(data: dict[str, Any], **slo: Any) -> dict[str, Any]:
    return {
        "litellm_params": {"metadata": data["metadata"]},
        "standard_logging_object": {"custom_llm_provider": "azure", **slo},
        "response_cost": 5.2e-06,
    }


async def test_missing_project_is_400_before_the_call(plugin: FinOpsPlugin) -> None:
    with pytest.raises(HTTPException) as exc:
        await plugin.async_pre_call_hook(key(), None, request(project=None), "acompletion")
    assert exc.value.status_code == 400
    detail = detail_of(exc.value)
    assert detail["error"] == "missing_attribution"
    assert detail["missing_headers"] == ["X-Project"]
    assert "X-Team" not in detail["hint"]


async def test_key_without_team_is_400(plugin: FinOpsPlugin) -> None:
    with pytest.raises(HTTPException) as exc:
        await plugin.async_pre_call_hook(key(team=None), None, request(), "acompletion")
    assert exc.value.status_code == 400
    assert detail_of(exc.value)["missing"] == ["team"]


async def test_unknown_model_is_422_before_the_call(plugin: FinOpsPlugin) -> None:
    with pytest.raises(HTTPException) as exc:
        await plugin.async_pre_call_hook(key(), None, request(model="mystery"), "acompletion")
    assert exc.value.status_code == 422
    assert "mystery" in str(exc.value.detail)


async def test_non_chat_calls_pass_through(plugin: FinOpsPlugin) -> None:
    data = request(model="mystery", project=None)
    assert await plugin.async_pre_call_hook(key(), None, data, "aembedding") is data


async def test_allowed_call_reserves_and_settles_in_the_headers_hook(
    plugin: FinOpsPlugin, ledger: Ledger
) -> None:
    data = await plugin.async_pre_call_hook(key(), None, request(), "acompletion")
    assert data is not None
    rid = data["metadata"][META]["request_id"]
    assert rid.startswith("lt-")
    assert ledger.counter("customer-support").reserved_usd > 0

    headers = await plugin.async_post_call_response_headers_hook(data, key(), response())
    assert headers is not None
    assert headers["X-FinOps-Request-Id"] == rid
    assert headers["X-FinOps-Currency"] == "USD"
    # 9 input at 0.15 / 1M + 1 output at 0.60 / 1M
    assert headers["X-FinOps-Cost-USD"] == "0.00000195"
    assert headers["X-FinOps-Budget-Status"] == "ok"
    counter = ledger.counter("customer-support")
    assert counter.reserved_usd == 0
    assert counter.spent_usd == Decimal("0.00000195")

    # The success event sees the request as settled and does not record it twice.
    await plugin.async_log_success_event(success_kwargs(data), response(), None, None)
    assert ledger.counter("customer-support").spent_usd == Decimal("0.00000195")
    assert plugin._pending == {}


async def test_streaming_settles_from_the_success_event(
    plugin: FinOpsPlugin, ledger: Ledger
) -> None:
    data = await plugin.async_pre_call_hook(key(), None, request(stream=True), "acompletion")
    assert data is not None
    stream_wrapper = SimpleNamespace()  # no usage yet
    headers = await plugin.async_post_call_response_headers_hook(data, key(), stream_wrapper)
    assert headers is not None
    assert "X-FinOps-Cost-USD" not in headers
    assert headers["X-FinOps-Budget-Status"] == "ok"
    assert ledger.counter("customer-support").reserved_usd > 0

    await plugin.async_log_success_event(
        success_kwargs(data, prompt_tokens=9, completion_tokens=1), response(), None, None
    )
    counter = ledger.counter("customer-support")
    assert counter.reserved_usd == 0
    assert counter.spent_usd == Decimal("0.00000195")


async def test_failure_releases_the_reservation(plugin: FinOpsPlugin, ledger: Ledger) -> None:
    data = await plugin.async_pre_call_hook(key(), None, request(), "acompletion")
    assert data is not None
    assert ledger.counter("customer-support").reserved_usd > 0
    await plugin.async_log_failure_event(success_kwargs(data), None, None, None)
    counter = ledger.counter("customer-support")
    assert counter.reserved_usd == 0 and counter.spent_usd == 0


async def test_cached_tokens_are_priced_at_the_cached_rate(
    plugin: FinOpsPlugin, ledger: Ledger
) -> None:
    data = await plugin.async_pre_call_hook(key(), None, request(), "acompletion")
    assert data is not None
    headers = await plugin.async_post_call_response_headers_hook(
        data, key(), response(prompt=100, completion=0, cached=100)
    )
    assert headers is not None
    assert headers["X-FinOps-Cost-USD"] == "0.0000075"  # 100 cached at 0.075 / 1M


async def test_budget_blocks_with_403_once_spent(plugin: FinOpsPlugin, ledger: Ledger) -> None:
    data = await plugin.async_pre_call_hook(key(), None, request(), "acompletion")
    assert data is not None
    await plugin.async_post_call_response_headers_hook(
        data, key(), response(prompt=4000, completion=4000)
    )  # 4000 * 0.15 + 4000 * 0.60 per million = 0.003 USD, over the 0.001 budget
    with pytest.raises(HTTPException) as exc:
        await plugin.async_pre_call_hook(key(), None, request(), "acompletion")
    assert exc.value.status_code == 403
    detail = detail_of(exc.value)
    assert detail["error"] == "budget_exceeded"
    assert detail["budget"]["level"] == "block"


async def test_warn_only_team_keeps_going_with_a_warning_header(plugin: FinOpsPlugin) -> None:
    k = key(team="research", alias="lab")
    data = await plugin.async_pre_call_hook(k, None, request(), "acompletion")
    assert data is not None
    await plugin.async_post_call_response_headers_hook(
        data, k, response(prompt=4000, completion=4000)
    )
    data2 = await plugin.async_pre_call_hook(k, None, request(), "acompletion")
    assert data2 is not None
    headers = await plugin.async_post_call_response_headers_hook(data2, k, response())
    assert headers is not None
    # enforcement: warn never blocks; the level stays "warn" past 100% and the header says so
    assert headers["X-FinOps-Budget-Status"] == "warn"
    assert "X-FinOps-Warning" in headers


async def test_hook_signatures_match_the_pinned_litellm(plugin: FinOpsPlugin) -> None:
    """Contract test: a LiteLLM upgrade that changes a hook's parameters fails here."""
    import inspect

    from litellm.integrations.custom_logger import CustomLogger

    for name in (
        "async_pre_call_hook",
        "async_post_call_response_headers_hook",
        "async_log_success_event",
        "async_log_failure_event",
    ):
        base = list(inspect.signature(getattr(CustomLogger, name)).parameters)
        ours = list(inspect.signature(getattr(FinOpsPlugin, name)).parameters)
        assert ours == base, f"{name}: plugin {ours} != litellm {base}"


REGISTRY_POLICIES = """\
version: "registry"
defaults:
  allowed_models: ["*"]
teams:
  product:
    budget: { monthly_usd: 1, warn_at: 0.8, block_at: 1.0, reserve_output_tokens: 10 }
    projects:
      chat-assistant:
        budget: { monthly_usd: 0.000005, warn_at: 0.5, block_at: 1.0, reserve_output_tokens: 10 }
"""


async def test_project_registry_and_project_budget_in_litellm_mode(
    tmp_path: Path, ledger: Ledger
) -> None:
    (tmp_path / "pricing.yaml").write_text(PRICING)
    (tmp_path / "policies.yaml").write_text(REGISTRY_POLICIES)
    p = FinOpsPlugin(
        price_book=PriceBook.from_yaml(tmp_path / "pricing.yaml"),
        policies=PolicySet.from_yaml(tmp_path / "policies.yaml"),
        ledger=ledger,
    )
    k = key(team="product", alias="app")
    with pytest.raises(HTTPException) as exc:
        await p.async_pre_call_hook(k, None, request(project="pilot-7"), "acompletion")
    assert exc.value.status_code == 403
    assert detail_of(exc.value)["error"] == "project_not_registered"

    data = await p.async_pre_call_hook(k, None, request(), "acompletion")
    assert data is not None
    assert ledger.counter("product/chat-assistant").reserved_usd > 0
    headers = await p.async_post_call_response_headers_hook(data, k, response())
    assert headers is not None
    assert headers["X-FinOps-Project-Budget-Limit-USD"] == "0.000005"
    assert headers["X-FinOps-Project-Budget-Status"] == "ok"
    assert headers["X-FinOps-Budget-Status"] == "ok"
    assert ledger.counter("product/chat-assistant").reserved_usd == 0
    assert ledger.counter("product/chat-assistant").spent_usd == Decimal("0.00000195")
