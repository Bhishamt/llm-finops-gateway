"""Export the ledger in the FinOps Open Cost and Usage Specification (FOCUS) format
(ADR-0023): one row per team, project, use case, provider, model, price book version and UTC
day, with the FOCUS 1.1 mandatory columns and ``x_`` extension columns for token detail."""

from __future__ import annotations

import csv
import io
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from .ledger import LedgerRow
from .pricing import money

COLUMNS = [
    "BilledCost",
    "EffectiveCost",
    "ListCost",
    "ContractedCost",
    "BillingCurrency",
    "BillingAccountId",
    "BillingAccountName",
    "BillingPeriodStart",
    "BillingPeriodEnd",
    "ChargePeriodStart",
    "ChargePeriodEnd",
    "ChargeCategory",
    "ChargeClass",
    "ChargeDescription",
    "ChargeFrequency",
    "InvoiceIssuerName",
    "ProviderName",
    "PublisherName",
    "ServiceCategory",
    "ServiceName",
    "SubAccountId",
    "SubAccountName",
    "ConsumedQuantity",
    "ConsumedUnit",
    "PricingQuantity",
    "PricingUnit",
    "ResourceId",
    "ResourceName",
    "ResourceType",
    "SkuId",
    "SkuPriceId",
    "Tags",
    "x_Calls",
    "x_InputTokens",
    "x_OutputTokens",
    "x_CachedInputTokens",
    "x_PriceBookVersion",
]

# provider as recorded in the ledger -> (ProviderName, ServiceName)
PROVIDERS: dict[str, tuple[str, str]] = {
    "azure_openai": ("Microsoft", "Azure OpenAI"),
    "azure": ("Microsoft", "Azure OpenAI"),
    "openai": ("OpenAI", "OpenAI API"),
    "openai_compat": ("OpenAI-compatible endpoint", "OpenAI-compatible endpoint"),
    "ollama": ("Self-hosted", "Ollama"),
    "anthropic": ("Anthropic", "Anthropic API"),
}


@dataclass(frozen=True)
class FocusRow:
    day: datetime  # UTC midnight
    team: str
    project: str
    use_case: str
    provider: str
    model: str
    price_book_version: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost: Decimal


def _day(ts: datetime) -> datetime:
    ts = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def aggregate(rows: list[LedgerRow]) -> list[FocusRow]:
    """Daily rows from ledger rows, in a stable order."""
    acc: OrderedDict[tuple[datetime, str, str, str, str, str, str], list[int | Decimal]] = (
        OrderedDict()
    )
    for r in sorted(rows, key=lambda r: (r.ts, r.team, r.project, r.model)):
        key = (_day(r.ts), r.team, r.project, r.use_case, r.provider, r.model, r.price_book_version)
        a = acc.setdefault(key, [0, 0, 0, 0, Decimal(0)])
        a[0] = int(a[0]) + 1
        a[1] = int(a[1]) + r.input_tokens
        a[2] = int(a[2]) + r.output_tokens
        a[3] = int(a[3]) + r.cached_input_tokens
        a[4] = Decimal(a[4]) + r.cost_usd
    return [
        FocusRow(
            day=k[0],
            team=k[1],
            project=k[2],
            use_case=k[3],
            provider=k[4],
            model=k[5],
            price_book_version=k[6],
            calls=int(v[0]),
            input_tokens=int(v[1]),
            output_tokens=int(v[2]),
            cached_input_tokens=int(v[3]),
            cost=Decimal(v[4]),
        )
        for k, v in acc.items()
    ]


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def focus_csv(
    rows: list[LedgerRow],
    currency: str,
    period_start: datetime,
    period_end: datetime,
    *,
    billing_account_id: str = "",
    billing_account_name: str = "",
) -> str:
    """The FOCUS CSV for ledger rows of one billing period (UTC)."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in aggregate(rows):
        provider_name, service_name = PROVIDERS.get(r.provider, (r.provider, r.provider))
        cost = money(r.cost)
        tokens = r.input_tokens + r.output_tokens
        w.writerow(
            {
                "BilledCost": cost,
                "EffectiveCost": cost,
                "ListCost": cost,
                "ContractedCost": cost,
                "BillingCurrency": currency,
                "BillingAccountId": billing_account_id,
                "BillingAccountName": billing_account_name,
                "BillingPeriodStart": _iso(period_start),
                "BillingPeriodEnd": _iso(period_end),
                "ChargePeriodStart": _iso(r.day),
                "ChargePeriodEnd": _iso(r.day + timedelta(days=1)),
                "ChargeCategory": "Usage",
                "ChargeClass": "",
                "ChargeDescription": f"{r.model} tokens via {service_name}",
                "ChargeFrequency": "Usage-Based",
                "InvoiceIssuerName": provider_name,
                "ProviderName": provider_name,
                "PublisherName": provider_name,
                "ServiceCategory": "AI and Machine Learning",
                "ServiceName": service_name,
                "SubAccountId": r.team,
                "SubAccountName": r.team,
                "ConsumedQuantity": tokens,
                "ConsumedUnit": "Tokens",
                "PricingQuantity": tokens,
                "PricingUnit": "Tokens",
                "ResourceId": r.model,
                "ResourceName": r.model,
                "ResourceType": "LLM model",
                "SkuId": r.model,
                "SkuPriceId": f"{r.model}@{r.price_book_version}",
                "Tags": json.dumps(
                    {"team": r.team, "project": r.project, "use_case": r.use_case},
                    separators=(",", ":"),
                ),
                "x_Calls": r.calls,
                "x_InputTokens": r.input_tokens,
                "x_OutputTokens": r.output_tokens,
                "x_CachedInputTokens": r.cached_input_tokens,
                "x_PriceBookVersion": r.price_book_version,
            }
        )
    return out.getvalue()
