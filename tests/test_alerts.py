from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from llm_finops_gateway.alerts import Alerter
from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import BudgetStatus, PolicySet
from llm_finops_gateway.pricing import PriceBook
from llm_finops_gateway.providers.base import ProviderError
from tests.conftest import ATTR_HEADERS, FakeProvider

HOOK = "https://hooks.example.test/T000/B000/xyz"


def _status(pct: str, level: str = "warn") -> BudgetStatus:
    return BudgetStatus(
        limit_usd=Decimal("500"),
        spent_usd=Decimal("412.10"),
        reserved_usd=Decimal("0"),
        used_pct=Decimal(pct),
        remaining_usd=Decimal("87.9"),
        level=level,  # type: ignore[arg-type]
        enforcement="block",
        warn_at_pct=Decimal("80"),
        block_at_pct=Decimal("100"),
    )


@respx.mock
async def test_budget_warn_posts_once_per_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.post(HOOK).mock(return_value=httpx.Response(200, text="ok"))
    alerter = Alerter(HOOK, cooldown_seconds=3600, currency="EUR")
    alerter.budget_warn("platform", _status("82.4"), "team 'platform' has used 82.4%")
    alerter.budget_warn("platform", _status("83.0"), "again")
    alerter.budget_warn("research", _status("55.0"), "other team")
    await alerter.aclose()
    assert route.call_count == 2 and alerter.sent == 2
    import json

    first = json.loads(route.calls[0].request.content)
    assert first["event"] == "budget_warn" and first["team"] == "platform"
    assert first["used_pct"] == "82.4" and first["limit"] == "500"
    assert "82.4 % of 500 EUR" in first["text"]
    # past the cooldown the same team alerts again
    import time

    monkeypatch.setattr(time, "monotonic", lambda: time.time() + 7200)
    alerter.budget_warn("platform", _status("90.0"), "later")
    await alerter.aclose()
    assert route.call_count == 3


@respx.mock
async def test_block_and_upstream_events_and_delivery_failure_is_swallowed() -> None:
    route = respx.post(HOOK).mock(
        side_effect=[httpx.Response(200), httpx.Response(500), httpx.ConnectError("down")]
    )
    alerter = Alerter(HOOK, cooldown_seconds=0)
    alerter.budget_block("platform", _status("100.0", "block"), "blocked")
    alerter.upstream_failure("azure_openai", "upstream_rate_limited", "gpt-4.1-mini", "429")
    alerter.upstream_failure("azure_openai", "upstream_error", "gpt-4.1-mini", "boom")
    await alerter.aclose()  # the 500 and the connection error are logged, not raised
    assert route.call_count == 3 and alerter.sent == 1


async def test_disabled_alerter_sends_nothing() -> None:
    alerter = Alerter(None)
    alerter.budget_warn("platform", _status("82.4"), "x")
    await alerter.aclose()
    assert not alerter.enabled and alerter.sent == 0


@respx.mock
def test_gateway_alerts_on_upstream_failure(
    pricing_path: Path, price_book: PriceBook, ledger: Ledger
) -> None:
    route = respx.post(HOOK).mock(return_value=httpx.Response(200))
    provider = FakeProvider()
    provider.fail_with = ProviderError("fake", "throttled", status_code=429, retry_after="1")
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    alerter = Alerter(HOOK, cooldown_seconds=3600)
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=provider,
        alerter=alerter,
    )
    body = {"model": "priced", "messages": [{"role": "user", "content": "hi"}]}
    with TestClient(app) as c:
        assert c.post("/v1/chat/completions", json=body, headers=ATTR_HEADERS).status_code == 429
        assert c.post("/v1/chat/completions", json=body, headers=ATTR_HEADERS).status_code == 429
    # shutdown drained the in-flight post; the second failure was inside the cooldown
    assert route.call_count == 1
    import json

    payload = json.loads(route.calls[0].request.content)
    assert payload["event"] == "upstream_rate_limited" and payload["provider"] == "fake"


@respx.mock
async def test_teams_format_is_an_adaptive_card_and_send_test_waits() -> None:
    route = respx.post(HOOK).mock(return_value=httpx.Response(202))
    alerter = Alerter(HOOK, cooldown_seconds=0, fmt="teams", currency="EUR")
    assert await alerter.send_test() is True
    import json

    payload = json.loads(route.calls.last.request.content)
    card = payload["attachments"][0]
    assert payload["type"] == "message"
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert card["content"]["type"] == "AdaptiveCard"
    assert "82.4 % of 500 EUR" in card["content"]["body"][0]["text"]
    assert {"title": "team", "value": "example"} in card["content"]["body"][1]["facts"]


@respx.mock
def test_cli_alerts_test_reports_delivery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from llm_finops_gateway.cli import main

    respx.post(HOOK).mock(return_value=httpx.Response(200))
    monkeypatch.setenv("LFG_ALERT_WEBHOOK_URL", HOOK)
    with pytest.raises(SystemExit) as exc:
        main(["alerts", "test"])
    assert exc.value.code == 0 and "delivered" in capsys.readouterr().out
