"""Reconcile the ledger with Azure Cost Management (ADR-0018).

    llm-finops-gateway ledger reconcile --resource <azure openai resource id> --day 2026-10-30

Cost Management is asked for one day at subscription scope, filtered to the resource, grouped
by meter (the API allows two aggregations per query, so it takes two: cost + usage quantity,
then USD cost). Each meter is classified with the same rules as ``pricing sync`` (model,
input / output / cached, global / regional / data zone) and compared with the ledger's token
sums and cost for that day. Tokens should match except for calls that bypassed the gateway;
cost matches when the price book carries the deployment's SKU price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from .ledger import Ledger, ModelUsage
from .pricing import PriceBook, money
from .pricing_sync import model_tokens, parse_meter

MANAGEMENT = "https://management.azure.com"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
API_VERSION = "2023-11-01"


@dataclass(frozen=True)
class MeterUsage:
    meter: str
    model: str | None  # price book model name, None when no row matches the meter
    field: str  # input | output | cached_input
    deployment: str  # global | regional | datazone
    tokens: int
    cost: Decimal  # billing currency
    cost_usd: Decimal
    currency: str


@dataclass(frozen=True)
class ModelLine:
    model: str
    ledger_calls: int
    ledger_input: int
    ledger_output: int
    ledger_cost: Decimal
    azure_input: int
    azure_output: int
    azure_cost_usd: Decimal
    deployments: tuple[str, ...]


@dataclass(frozen=True)
class ReconcileReport:
    day: date
    currency: str
    lines: list[ModelLine]
    unmatched: list[MeterUsage] = field(default_factory=list)

    @property
    def ledger_cost(self) -> Decimal:
        return sum((line.ledger_cost for line in self.lines), start=Decimal(0))

    @property
    def azure_cost_usd(self) -> Decimal:
        return sum((line.azure_cost_usd for line in self.lines), start=Decimal(0)) + sum(
            (m.cost_usd for m in self.unmatched), start=Decimal(0)
        )


def _query(
    day: date, resource_id: str, aggregation: dict[str, Any], grouping: list[str]
) -> dict[str, Any]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start.isoformat().replace("+00:00", "Z"),
            "to": end.isoformat().replace("+00:00", "Z"),
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": aggregation,
            "grouping": [{"type": "Dimension", "name": g} for g in grouping],
            "filter": {
                "dimensions": {"name": "ResourceId", "operator": "In", "values": [resource_id]}
            },
        },
    }


def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    cols = [c["name"] for c in result["properties"]["columns"]]
    return [dict(zip(cols, r, strict=True)) for r in result["properties"]["rows"]]


def fetch_cost_management(
    resource_id: str, day: date, token: str, client: httpx.Client | None = None
) -> list[MeterUsage]:
    """One day of the resource's meters from the Cost Management Query API."""
    subscription = resource_id.split("/subscriptions/")[1].split("/")[0]
    url = f"{MANAGEMENT}/subscriptions/{subscription}/providers/Microsoft.CostManagement/query"
    own = client is None
    client = client or httpx.Client(timeout=60.0)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        q1 = client.post(
            url,
            params={"api-version": API_VERSION},
            headers=headers,
            json=_query(
                day,
                resource_id,
                {
                    "totalCost": {"name": "Cost", "function": "Sum"},
                    "usage": {"name": "UsageQuantity", "function": "Sum"},
                },
                ["Meter", "UnitOfMeasure"],
            ),
        )
        q1.raise_for_status()
        q2 = client.post(
            url,
            params={"api-version": API_VERSION},
            headers=headers,
            json=_query(
                day,
                resource_id,
                {"totalCostUSD": {"name": "CostUSD", "function": "Sum"}},
                ["Meter"],
            ),
        )
        q2.raise_for_status()
    finally:
        if own:
            client.close()
    usd = {r["Meter"]: Decimal(str(r["CostUSD"])) for r in _rows(q2.json())}
    out: list[MeterUsage] = []
    for r in _rows(q1.json()):
        unit = str(r.get("UnitOfMeasure", ""))
        mult = {"1K": 1000, "1M": 1_000_000}.get(unit)
        if mult is None:
            continue
        meter = str(r["Meter"])
        parsed = parse_meter(
            {
                "type": "Consumption",
                "productName": "Azure OpenAI",
                "meterName": meter,
                "unitOfMeasure": unit,
                "retailPrice": 0,
            }
        )
        if parsed is None:
            continue
        out.append(
            MeterUsage(
                meter=meter,
                model=None,
                field=parsed.field,
                deployment=parsed.deployment,
                tokens=round(float(r["UsageQuantity"]) * mult),
                cost=Decimal(str(r["Cost"])),
                cost_usd=usd.get(meter, Decimal(0)),
                currency=str(r.get("Currency", "")),
            )
        )
    return out


def reconcile(
    ledger: Ledger, book: PriceBook, meters: list[MeterUsage], day: date
) -> ReconcileReport:
    """Match meters to price book rows by model name and compare with the ledger's day."""
    by_tokens = {model_tokens(m): m for m in book.models()}
    matched: list[MeterUsage] = []
    unmatched: list[MeterUsage] = []
    for m in meters:
        parsed = parse_meter(
            {
                "type": "Consumption",
                "productName": "Azure OpenAI",
                "meterName": m.meter,
                "unitOfMeasure": "1K",
                "retailPrice": 0,
            }
        )
        name = by_tokens.get(parsed.model_tokens) if parsed else None
        if name is None:
            unmatched.append(m)
        else:
            matched.append(MeterUsage(**{**m.__dict__, "model": name}))
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    usage = {u.model: u for u in ledger.usage_by_model(start, start + timedelta(days=1))}
    models = sorted(set(usage) | {m.model for m in matched if m.model})
    lines: list[ModelLine] = []
    for model in models:
        u = usage.get(model, ModelUsage(model, 0, 0, 0, 0, Decimal(0)))
        mine = [m for m in matched if m.model == model]
        lines.append(
            ModelLine(
                model=model,
                ledger_calls=u.calls,
                ledger_input=u.input_tokens,
                ledger_output=u.output_tokens,
                ledger_cost=u.cost_usd,
                azure_input=sum(m.tokens for m in mine if m.field in ("input", "cached_input")),
                azure_output=sum(m.tokens for m in mine if m.field == "output"),
                azure_cost_usd=sum((m.cost_usd for m in mine), start=Decimal(0)),
                deployments=tuple(sorted({m.deployment for m in mine})),
            )
        )
    currency = next((m.currency for m in meters if m.currency), "USD")
    return ReconcileReport(day=day, currency=currency, lines=lines, unmatched=unmatched)


def format_report(report: ReconcileReport) -> str:
    lines = [
        f"{report.day.isoformat()} (UTC): ledger vs Azure Cost Management",
        f"{'model':14} {'calls':>5} {'in ledger':>10} {'in azure':>9} "
        f"{'out ledger':>11} {'out azure':>10} {'cost ledger':>12} {'cost azure':>11}  sku",
    ]
    for line in report.lines:
        lines.append(
            f"{line.model:14} {line.ledger_calls:>5} {line.ledger_input:>10} {line.azure_input:>9} "
            f"{line.ledger_output:>11} {line.azure_output:>10} {money(line.ledger_cost):>12} "
            f"{money(line.azure_cost_usd):>11}  {', '.join(line.deployments) or '-'}"
        )
    d_in = sum(line.azure_input - line.ledger_input for line in report.lines)
    d_out = sum(line.azure_output - line.ledger_output for line in report.lines)
    lines.append("")
    lines.append(
        f"tokens: Azure - ledger = {d_in:+} input, {d_out:+} output "
        "(positive = calls that did not pass through the gateway)"
    )
    lines.append(
        f"cost USD: Azure {money(report.azure_cost_usd)} vs ledger {money(report.ledger_cost)} "
        f"(delta {money(report.azure_cost_usd - report.ledger_cost)})"
    )
    regional = [line.model for line in report.lines if any(d != "global" for d in line.deployments)]
    if regional:
        lines.append(
            f"note: {', '.join(regional)} billed on a non-global SKU; the price book must "
            "carry that SKU's price (`pricing sync --deployment regional|datazone`) "
            "for the cost to match"
        )
    for m in report.unmatched:
        lines.append(f"unmatched meter: {m.meter} ({m.tokens} tokens, {money(m.cost_usd)} USD)")
    return "\n".join(lines)
