"""Versioned price book.

Design decision (ADR-0002): a model without a configured price raises ``UnknownModelError``.
Silently costing zero would hide spend, which is the exact failure a FinOps gateway exists
to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: Decimal
    output_per_million: Decimal


class UnknownModelError(KeyError):
    def __init__(self, model: str) -> None:
        super().__init__(f"No price configured for model '{model}'")
        self.model = model

    def __str__(self) -> str:
        return f"No price configured for model '{self.model}'"


class PriceBookError(ValueError):
    """Raised when the price book file is malformed."""


def money(value: Decimal | int | str) -> str:
    """Canonical string for a USD amount: no exponent, no trailing zeros ('0', '0.00075')."""
    d = Decimal(str(value)).normalize()
    if d == d.to_integral():
        return str(d.quantize(Decimal(1)))
    return format(d, "f")


class PriceBook:
    """Immutable, versioned price list loaded from YAML."""

    def __init__(self, prices: dict[str, ModelPrice], version: str, currency: str) -> None:
        self._prices = dict(prices)
        self.version = version
        self.currency = currency

    @classmethod
    def from_yaml(cls, path: Path) -> PriceBook:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise PriceBookError(f"{path}: top level must be a mapping")
        for key in ("version", "currency", "models"):
            if key not in raw:
                raise PriceBookError(f"{path}: missing required key '{key}'")
        if not isinstance(raw["models"], dict):
            raise PriceBookError(f"{path}: 'models' must be a mapping")
        prices: dict[str, ModelPrice] = {}
        for name, spec in raw["models"].items():
            if not isinstance(spec, dict) or "input" not in spec or "output" not in spec:
                raise PriceBookError(f"{path}: model '{name}' needs 'input' and 'output'")
            prices[str(name)] = ModelPrice(
                input_per_million=Decimal(str(spec["input"])),
                output_per_million=Decimal(str(spec["output"])),
            )
        return cls(prices, version=str(raw["version"]), currency=str(raw["currency"]))

    def models(self) -> list[str]:
        return sorted(self._prices)

    def has(self, model: str) -> bool:
        return model in self._prices

    def cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        try:
            price = self._prices[model]
        except KeyError as exc:
            raise UnknownModelError(model) from exc
        per_million = Decimal(1_000_000)
        return (
            Decimal(input_tokens) * price.input_per_million
            + Decimal(output_tokens) * price.output_per_million
        ) / per_million
