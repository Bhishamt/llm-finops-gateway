from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from llm_finops_gateway.pricing import PriceBook, PriceBookError, UnknownModelError, money


def test_cost_is_exact_decimal(price_book: PriceBook) -> None:
    assert price_book.cost("priced", 1_000_000, 100_000) == Decimal("3.5")


def test_cost_small_call_keeps_precision(price_book: PriceBook) -> None:
    assert price_book.cost("priced", 123, 45) == Decimal("0.0007575")


def test_free_model_costs_zero(price_book: PriceBook) -> None:
    assert price_book.cost("free", 10_000, 10_000) == Decimal("0")


def test_unknown_model_fails_loudly(price_book: PriceBook) -> None:
    with pytest.raises(UnknownModelError) as exc:
        price_book.cost("nope", 1, 1)
    assert "nope" in str(exc.value)


def test_negative_tokens_rejected(price_book: PriceBook) -> None:
    with pytest.raises(ValueError):
        price_book.cost("priced", -1, 0)


def test_version_and_models(price_book: PriceBook) -> None:
    assert price_book.version == "test-1"
    assert price_book.currency == "USD"
    assert price_book.models() == ["free", "priced"]
    assert price_book.has("priced") and not price_book.has("nope")


@pytest.mark.parametrize(
    "content",
    [
        "version: 1\ncurrency: USD\n",  # missing models
        "version: 1\ncurrency: USD\nmodels: [a, b]\n",  # models not a mapping
        "version: 1\ncurrency: USD\nmodels:\n  m: {input: 1}\n",  # missing output
        "- not\n- a\n- mapping\n",
    ],
)
def test_malformed_price_book_rejected(tmp_path: Path, content: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(content)
    with pytest.raises(PriceBookError):
        PriceBook.from_yaml(path)


def test_repo_price_book_loads() -> None:
    book = PriceBook.from_yaml(Path(__file__).parent.parent / "config" / "pricing.yaml")
    assert book.has("gpt-4o-mini")
    assert book.cost("gpt-4o-mini", 1_000_000, 0) == Decimal("0.15")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), "0"),
        (Decimal("0E-8"), "0"),
        (Decimal("0.0"), "0"),
        (Decimal("0.00075"), "0.00075"),
        (Decimal("1.5000"), "1.5"),
        (Decimal("500"), "500"),
        (Decimal("0.15"), "0.15"),
        (1, "1"),
    ],
)
def test_money_formatting(value: Decimal | int, expected: str) -> None:
    assert money(value) == expected
