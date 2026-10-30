from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from llm_finops_gateway.providers.base import CompletionResult

PRICING_YAML = """\
version: test-1
currency: USD
models:
  priced: {input: 2.5, output: 10, cached_input: 1.25}
  free: {input: 0, output: 0}
"""

ATTR_HEADERS = {"X-Team": "platform", "X-Project": "demo", "X-Use-Case": "unit-test"}


class FakeProvider:
    """Deterministic provider for proxy tests; records what it was asked."""

    name = "fake"

    def __init__(
        self, input_tokens: int = 100, output_tokens: int = 50, cached_input_tokens: int = 0
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_input_tokens = cached_input_tokens
        self.calls: list[dict[str, object]] = []
        self.closed = False
        self.fail_with: Exception | None = None

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        return CompletionResult(
            content="hello from fake",
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cached_input_tokens=self.cached_input_tokens,
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def pricing_path(tmp_path: Path) -> Path:
    path = tmp_path / "pricing.yaml"
    path.write_text(PRICING_YAML)
    return path


@pytest.fixture
def price_book(pricing_path: Path) -> PriceBook:
    return PriceBook.from_yaml(pricing_path)


@pytest.fixture
def ledger() -> Ledger:
    return Ledger("sqlite:///:memory:")


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def policies() -> PolicySet:
    """Default for the HTTP fixtures: no budgets, everything priced is allowed."""
    return PolicySet.empty()


@pytest.fixture
def client(
    pricing_path: Path,
    price_book: PriceBook,
    policies: PolicySet,
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
        policies=policies,
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        yield c
