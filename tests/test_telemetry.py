from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from llm_finops_gateway.telemetry import Telemetry
from tests.conftest import ATTR_HEADERS, FakeProvider

otel = pytest.importorskip("opentelemetry.sdk", reason="[otel] extra not installed")


def test_disabled_telemetry_is_a_noop() -> None:
    t = Telemetry(enabled=False, service_name="x")
    assert t.enabled is False
    t.annotate(team="a")  # must not raise
    t.shutdown()


def test_request_span_carries_finops_attributes(
    pricing_path: Path,
    price_book: PriceBook,
    ledger: Ledger,
    fake_provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    # create_app builds its own Telemetry; swap the class for one that uses our exporter.
    import llm_finops_gateway.main as main_mod

    monkeypatch.setattr(
        main_mod, "Telemetry", lambda enabled, name: Telemetry(enabled, name, exporter=exporter)
    )
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        otel_enabled=True,
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "priced", "messages": [{"role": "user", "content": "hi"}]},
            headers=ATTR_HEADERS,
        )
        assert r.status_code == 200
        c.get("/healthz")  # excluded from tracing

    spans = exporter.get_finished_spans()
    request_spans = [s for s in spans if s.attributes and "finops.team" in s.attributes]
    assert len(request_spans) == 1
    attrs = dict(request_spans[0].attributes or {})
    assert attrs["finops.team"] == "platform"
    assert attrs["finops.model"] == "priced"
    assert attrs["finops.cost_usd"] == pytest.approx(0.00075)
    assert attrs["finops.input_tokens"] == 100
    assert str(attrs["finops.request_id"]).startswith("gw-")
    assert not any(s.attributes and s.attributes.get("http.target") == "/healthz" for s in spans)
