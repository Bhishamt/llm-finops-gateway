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
        "version: 1\ncurrency: dollars\nmodels:\n  m: {input: 1, output: 1}\n",  # bad currency
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


def test_cached_input_tokens_billed_at_cached_price(price_book: PriceBook) -> None:
    # 60 fresh * 2.5 + 40 cached * 1.25 + 50 out * 10 = 150 + 50 + 500 per 1M
    assert price_book.cost("priced", 100, 50, cached_input_tokens=40) == Decimal("0.0007")


def test_cached_input_without_cached_price_costs_input_price(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("version: 1\ncurrency: USD\nmodels:\n  m: {input: 2, output: 4}\n")
    book = PriceBook.from_yaml(path)
    assert book.cost("m", 100, 0, cached_input_tokens=100) == book.cost("m", 100, 0)


def test_cached_tokens_cannot_exceed_input(price_book: PriceBook) -> None:
    with pytest.raises(ValueError):
        price_book.cost("priced", 10, 0, cached_input_tokens=11)


def test_provider_field_is_optional(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: 1\ncurrency: USD\nmodels:\n"
        "  a: {input: 1, output: 1, provider: azure_openai}\n  b: {input: 1, output: 1}\n"
    )
    book = PriceBook.from_yaml(path)
    assert book.provider_for("a") == "azure_openai"
    assert book.provider_for("b") is None
    with pytest.raises(UnknownModelError):
        book.provider_for("nope")


def test_reasoning_flag_defaults_to_false(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: 1\ncurrency: USD\nmodels:\n"
        "  r: {input: 1, output: 1, reasoning: true}\n  c: {input: 1, output: 1}\n"
    )
    book = PriceBook.from_yaml(path)
    assert book.is_reasoning("r") and not book.is_reasoning("c")
