from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from llm_finops_gateway.ledger import Ledger
from tests.conftest import ATTR_HEADERS, FakeProvider

BODY = {"model": "priced", "messages": [{"role": "user", "content": "hi"}]}


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["price_book"] == "test-1"


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


def test_streaming_not_supported(client: TestClient) -> None:
    r = client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=ATTR_HEADERS)
    assert r.status_code == 400


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
