from __future__ import annotations

import json
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from llm_finops_gateway.providers.base import ProviderError
from tests.conftest import ATTR_HEADERS, FakeProvider

BODY = {"model": "priced", "messages": [{"role": "user", "content": "hi"}]}


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["price_book"] == "test-1"
    # the string in __init__.py is bumped by hand; it must match the version in pyproject.toml
    pyproject = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert r.json()["version"] == pyproject["project"]["version"]


def test_models_lists_price_book(client: TestClient) -> None:
    r = client.get("/v1/models")
    assert [m["id"] for m in r.json()["data"]] == ["free", "priced"]


def test_chat_completion_is_openai_shaped_and_priced(
    client: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    r = client.post("/v1/chat/completions", json=BODY, headers=ATTR_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("gw-")
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "hello from fake"}
    assert body["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    # 100 * 2.5/1M + 50 * 10/1M
    assert Decimal(body["x_finops"]["cost_usd"]) == Decimal("0.00075")
    assert body["x_finops"]["team"] == "platform"
    assert body["x_finops"]["price_book_version"] == "test-1"

    assert fake_provider.calls[0]["model"] == "priced"
    rows = ledger.summary()
    assert len(rows) == 1 and rows[0].calls == 1 and rows[0].cost_usd == Decimal("0.00075")


def test_missing_attribution_is_rejected_before_upstream_call(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    r = client.post("/v1/chat/completions", json=BODY, headers={"X-Team": "platform"})
    assert r.status_code == 400
    assert r.json()["detail"]["missing_headers"] == ["X-Project"]
    assert fake_provider.calls == []


def test_unknown_model_is_rejected_before_upstream_call(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    r = client.post(
        "/v1/chat/completions", json={**BODY, "model": "unpriced"}, headers=ATTR_HEADERS
    )
    assert r.status_code == 422
    assert "unpriced" in r.json()["detail"]
    assert fake_provider.calls == []


def _events(text: str) -> list[Any]:
    out: list[Any] = []
    for block in text.strip().split("\n\n"):
        assert block.startswith("data: "), block
        payload = block[len("data: ") :]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def test_streaming_emits_openai_chunks_and_settles_cost_in_final_event(
    client: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    r = client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=ATTR_HEADERS)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["X-FinOps-Request-Id"].startswith("gw-")
    events = _events(r.text)
    assert events[-1] == "[DONE]"
    chunks = [e for e in events[:-1] if e["choices"]]
    assert all(e["object"] == "chat.completion.chunk" for e in chunks)
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "hello from fake"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    final = events[-2]
    assert final["choices"] == []
    assert final["usage"] == {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    assert Decimal(final["x_finops"]["cost_usd"]) == Decimal("0.00075")
    assert final["x_finops"]["team"] == "platform"
    assert "usage_estimated" not in final
    assert fake_provider.calls[0]["stream"] is True
    rows = ledger.summary()
    assert len(rows) == 1 and rows[0].calls == 1 and rows[0].cost_usd == Decimal("0.00075")


def test_streaming_failure_emits_error_event_and_records_nothing(
    client: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    fake_provider.fail_with = ProviderError("fake", "boom", status_code=503)
    fake_provider.fail_mid_stream = True
    r = client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=ATTR_HEADERS)
    assert r.status_code == 200  # headers were already sent; the error travels in the stream
    events = _events(r.text)
    assert events[0]["choices"][0]["delta"]["content"] == "hello "
    assert events[-2]["error"] == "upstream_error" and events[-2]["provider"] == "fake"
    assert events[-1] == "[DONE]"
    assert ledger.summary() == []


def test_streaming_without_usage_bills_the_estimate(
    client: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    fake_provider.stream_without_usage = True
    body = {**BODY, "stream": True, "max_tokens": 10}
    r = client.post("/v1/chat/completions", json=body, headers=ATTR_HEADERS)
    final = _events(r.text)[-2]
    assert final["usage_estimated"] is True
    # "hi" -> 0 chars/4 + 8 per message = 8 input tokens; output bounded by max_tokens
    assert final["usage"] == {"prompt_tokens": 8, "completion_tokens": 10, "total_tokens": 18}
    rows = ledger.summary()
    assert rows[0].input_tokens == 8 and rows[0].output_tokens == 10


def test_generation_params_are_forwarded(client: TestClient, fake_provider: FakeProvider) -> None:
    r = client.post(
        "/v1/chat/completions",
        json={**BODY, "temperature": 0.2, "max_tokens": 32},
        headers=ATTR_HEADERS,
    )
    assert r.status_code == 200
    assert fake_provider.calls[0]["temperature"] == 0.2
    assert fake_provider.calls[0]["max_tokens"] == 32


def test_summary_groups_by_team_project_model(client: TestClient) -> None:
    client.post("/v1/chat/completions", json=BODY, headers=ATTR_HEADERS)
    client.post("/v1/chat/completions", json=BODY, headers=ATTR_HEADERS)
    client.post(
        "/v1/chat/completions",
        json={**BODY, "model": "free"},
        headers={**ATTR_HEADERS, "X-Team": "research"},
    )
    r = client.get("/v1/finops/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["currency"] == "USD"
    assert len(body["rows"]) == 2
    platform = next(x for x in body["rows"] if x["team"] == "platform")
    assert platform["calls"] == 2
    assert Decimal(platform["cost_usd"]) == Decimal("0.0015")
    assert Decimal(body["total_cost_usd"]) == Decimal("0.0015")

    r = client.get("/v1/finops/summary", params={"team": "research"})
    assert [x["team"] for x in r.json()["rows"]] == ["research"]


def test_provider_is_closed_on_shutdown(fake_provider: FakeProvider, client: TestClient) -> None:
    assert fake_provider.closed is False
    client.__exit__(None, None, None)
    assert fake_provider.closed is True


def test_zero_cost_is_rendered_as_plain_zero(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    """Local models cost 0; headers, body and summary must all say '0', never '0.0' or '0E-7'."""
    r = client.post("/v1/chat/completions", json={**BODY, "model": "free"}, headers=ATTR_HEADERS)
    assert r.status_code == 200
    assert r.headers["X-FinOps-Cost-USD"] == "0"
    assert r.json()["x_finops"]["cost_usd"] == "0"
    summary = client.get("/v1/finops/summary").json()
    assert summary["rows"][0]["cost_usd"] == "0" and summary["total_cost_usd"] == "0"


def test_upstream_rate_limit_is_passed_through_as_429(
    client: TestClient, fake_provider: FakeProvider, ledger: Ledger
) -> None:
    fake_provider.fail_with = ProviderError("fake", "throttled", status_code=429, retry_after="7")
    r = client.post("/v1/chat/completions", json=BODY, headers=ATTR_HEADERS)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "7"
    assert r.json()["error"] == "upstream_rate_limited"
    assert ledger.summary() == []


def test_cached_input_tokens_are_priced_and_reported(
    pricing_path: Path, price_book: PriceBook, ledger: Ledger
) -> None:
    provider = FakeProvider(input_tokens=100, output_tokens=50, cached_input_tokens=40)
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=provider,
    )
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json=BODY, headers=ATTR_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 40}
    assert Decimal(body["x_finops"]["cost_usd"]) == Decimal("0.0007")
    assert r.headers["X-FinOps-Cached-Input-Tokens"] == "40"


def test_price_book_provider_routes_each_model(tmp_path: Path, ledger: Ledger) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: r\ncurrency: USD\nmodels:\n"
        "  priced: {input: 1, output: 1}\n"
        "  routed: {input: 1, output: 1, provider: fake2}\n"
        "  orphan: {input: 1, output: 1, provider: nowhere}\n"
    )
    default, second = FakeProvider(), FakeProvider()
    second.name = "fake2"
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=PriceBook.from_yaml(path),
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=default,
        providers={"fake2": second},
    )
    with TestClient(app) as c:
        routed = c.post(
            "/v1/chat/completions", json={**BODY, "model": "routed"}, headers=ATTR_HEADERS
        )
        orphan = c.post(
            "/v1/chat/completions", json={**BODY, "model": "orphan"}, headers=ATTR_HEADERS
        )
        models = c.get("/v1/models").json()["data"]
        health = c.get("/healthz").json()
    assert routed.json()["x_finops"]["provider"] == "fake2"
    assert second.calls[0]["model"] == "routed"
    assert orphan.json()["x_finops"]["provider"] == "fake"  # unconfigured -> default
    assert default.calls[0]["model"] == "orphan"
    assert {m["id"]: m["owned_by"] for m in models} == {
        "orphan": "fake",
        "priced": "fake",
        "routed": "fake2",
    }
    assert (health["providers"], health["default_provider"]) == ("fake,fake2", "fake")
    assert second.closed and default.closed


def test_reasoning_model_translates_generation_params(tmp_path: Path, ledger: Ledger) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: r\ncurrency: USD\nmodels:\n  thinker: {input: 1, output: 1, reasoning: true}\n"
    )
    provider = FakeProvider(output_tokens=40, reasoning_tokens=40)
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=PriceBook.from_yaml(path),
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=provider,
    )
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={**BODY, "model": "thinker", "temperature": 0.2, "max_tokens": 40},
            headers=ATTR_HEADERS,
        )
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["completion_tokens_details"] == {"reasoning_tokens": 40}
    call = provider.calls[0]
    assert (call["temperature"], call["max_tokens"], call["max_completion_tokens"]) == (
        None,
        None,
        40,
    )


TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}
CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "get_weather", "arguments": '{"city": "Budapest"}'},
}


def test_tools_are_forwarded_and_tool_calls_returned(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    fake_provider.tool_calls = [CALL]
    body = {
        **BODY,
        "tools": [TOOL],
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
        "top_p": 0.9,
        "seed": 7,
        "user": "u-1",
    }
    r = client.post("/v1/chat/completions", json=body, headers=ATTR_HEADERS)
    assert r.status_code == 200, r.text
    assert fake_provider.calls[0]["extra"] == {
        "tools": [TOOL],
        "tool_choice": "auto",
        "response_format": {"type": "json_object"},
        "top_p": 0.9,
        "seed": 7,
        "user": "u-1",
    }
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"] == {"role": "assistant", "content": None, "tool_calls": [CALL]}


def test_unknown_request_field_is_rejected_loudly(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    r = client.post("/v1/chat/completions", json={**BODY, "foo": 1}, headers=ATTR_HEADERS)
    assert r.status_code == 422
    assert "foo" in r.text
    assert fake_provider.calls == []


def test_tool_results_and_content_parts_pass_through(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "weather?"}]},
        {"role": "assistant", "content": None, "tool_calls": [CALL]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ]
    r = client.post(
        "/v1/chat/completions", json={**BODY, "messages": messages}, headers=ATTR_HEADERS
    )
    assert r.status_code == 200, r.text
    assert fake_provider.calls[0]["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "weather?"}]},
        {"role": "assistant", "tool_calls": [CALL]},
        {"role": "tool", "content": "sunny", "tool_call_id": "call_1"},
    ]


def test_streaming_forwards_tool_call_deltas(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    fake_provider.tool_calls = [CALL]
    r = client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=ATTR_HEADERS)
    events = _events(r.text)
    assert events[0]["choices"][0]["delta"]["tool_calls"] == [{"index": 0, **CALL}]
    assert events[-1] == "[DONE]"


def test_max_completion_tokens_is_accepted_from_clients(
    client: TestClient, fake_provider: FakeProvider
) -> None:
    body = {**BODY, "max_completion_tokens": 33}
    r = client.post("/v1/chat/completions", json=body, headers=ATTR_HEADERS)
    assert r.status_code == 200, r.text
    assert fake_provider.calls[0]["max_completion_tokens"] == 33
    r = client.post("/v1/chat/completions", json={**BODY, "n": 2}, headers=ATTR_HEADERS)
    assert r.status_code == 422


def test_currency_is_reported_in_body_and_header(client: TestClient) -> None:
    r = client.post("/v1/chat/completions", json=BODY, headers=ATTR_HEADERS)
    assert r.json()["x_finops"]["currency"] == "USD"
    assert r.headers["X-FinOps-Currency"] == "USD"
