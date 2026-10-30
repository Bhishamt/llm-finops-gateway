"""Price book sync against the Azure Retail Prices API (ADR-0013).

The price book stays a reviewed file in git; this module only tells you where it drifted from
the public list price and, on request, rewrites the numbers in place so the change can go
through a pull request. Nothing here runs at request time.

    llm-finops-gateway pricing sync --region swedencentral [--deployment global] [--write]
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml

from .pricing import money

RETAIL_URL = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"
DEPLOYMENTS = ("global", "regional", "datazone")
FIELDS = ("input", "output", "cached_input")

_DIRECTION = {
    "inp": "input",
    "inpt": "input",
    "input": "input",
    "outp": "output",
    "outpt": "output",
    "output": "output",
}
_CACHED = {"cached", "cchd"}
_DEPLOYMENT = {
    "glbl": "global",
    "global": "global",
    "regnl": "regional",
    "regional": "regional",
    "rgnl": "regional",
    "dzone": "datazone",
    "dzn": "datazone",
    "datazone": "datazone",
}
# Meters that are not plain chat-completion token prices.
_EXCLUDE = {
    "batch",
    "ft",
    "training",
    "hosting",
    "grdr",
    "pp",
    "aud",
    "audio",
    "rt",
    "rtime",
    "realtime",
    "transcribe",
    "tts",
    "txt",
    "image",
    "img",
    "mdl",
    "fine",
    "tune",
    "cd",
}
_VERSION = re.compile(r"^\d{4}$")  # MMDD of the model version, e.g. 1120 for 2024-11-20


@dataclass(frozen=True)
class Meter:
    model_tokens: tuple[str, ...]
    version: str | None
    field: str  # input | output | cached_input
    deployment: str  # global | regional | datazone
    price_per_million: Decimal
    meter_name: str


@dataclass(frozen=True)
class RowResult:
    model: str
    field: str
    current: Decimal | None
    retail: Decimal | None
    status: str  # ok | differs | missing | unmatched | ambiguous
    note: str = ""


def model_tokens(name: str) -> tuple[str, ...]:
    return tuple(re.sub(r"[-_]", " ", name.lower()).split())


def parse_meter(item: dict[str, Any]) -> Meter | None:
    """Classify one Retail Prices item; None when it is not a chat token meter.

    Azure spells the same thing several ways: ``gpt 4.1 mini Inp glbl Tokens``,
    ``gpt-4o-mini-0718-Outp-regnl Tokens``, ``GPT 5 Mini cchd Inpt Glbl 1M Tokens``,
    ``gpt 4o 0513 Input Data Zone Tokens``. Prices are per 1K or per 1M tokens.
    """
    if item.get("type") != "Consumption":
        return None
    if not str(item.get("productName", "")).startswith("Azure OpenAI"):
        return None
    unit = item.get("unitOfMeasure")
    factor = {"1K": 1000, "1M": 1}.get(str(unit))
    if factor is None:
        return None
    name = str(item.get("meterName", ""))
    tokens = re.sub(r"[-_]", " ", name.lower()).split()
    if not tokens or tokens[-1] != "tokens":
        return None
    tokens = [t for t in tokens[:-1] if t != "1m"]
    if _EXCLUDE & set(tokens):
        return None
    direction: str | None = None
    deployment: str | None = None
    cached = False
    rest: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "data" and i + 1 < len(tokens) and tokens[i + 1] == "zone":
            deployment = "datazone"
            i += 2
            continue
        if t in _DIRECTION:
            direction = _DIRECTION[t]
        elif t in _CACHED:
            cached = True
        elif t in _DEPLOYMENT:
            deployment = _DEPLOYMENT[t]
        else:
            rest.append(t)
        i += 1
    if direction is None or deployment is None or (cached and direction != "input"):
        return None
    version: str | None = None
    model: list[str] = []
    for t in rest:
        if _VERSION.match(t):
            version = t
        else:
            model.append(t)
    if not model:
        return None
    return Meter(
        model_tokens=tuple(model),
        version=version,
        field="cached_input" if cached else direction,
        deployment=deployment,
        price_per_million=Decimal(str(item["retailPrice"])) * factor,
        meter_name=name,
    )


def fetch_retail_items(
    region: str, currency: str = "USD", client: httpx.Client | None = None
) -> list[dict[str, Any]]:
    """All Foundry Models price items for a region, priced in ``currency`` (the API pages by
    1000 and converts with Microsoft's own exchange rates)."""
    own = client is None
    client = client or httpx.Client(timeout=60.0)
    items: list[dict[str, Any]] = []
    try:
        url: str | None = RETAIL_URL
        params: dict[str, str] | None = {
            "api-version": API_VERSION,
            "currencyCode": f"'{currency}'",
            "$filter": f"armRegionName eq '{region}' and serviceName eq 'Foundry Models'",
        }
        while url:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            items.extend(data.get("Items") or [])
            url = data.get("NextPageLink") or None
            params = None
    finally:
        if own:
            client.close()
    return items


def _hint_mmdd(version: str | None) -> str | None:
    """'2024-11-20' -> '1120'; a bare '1120' is kept."""
    if version is None:
        return None
    digits = re.sub(r"\D", "", str(version))
    return digits[-4:] if len(digits) >= 4 else None


def resolve(
    meters: list[Meter], model: str, deployment: str, version: str | None
) -> dict[str, Decimal | list[str]]:
    """Retail price per field for one model, or the list of candidate versions when the
    meters disagree and the row does not say which model version it means."""
    want = model_tokens(model)
    hint = _hint_mmdd(version)
    out: dict[str, Decimal | list[str]] = {}
    for field in FIELDS:
        cands = [
            m
            for m in meters
            if m.model_tokens == want and m.field == field and m.deployment == deployment
        ]
        if hint is not None:
            cands = [m for m in cands if m.version in (None, hint)]
        prices = {m.price_per_million.normalize() for m in cands}
        if not prices:
            continue
        if len(prices) > 1:
            out[field] = sorted({m.version or "-" for m in cands})
        else:
            out[field] = prices.pop()
    return out


def compare(
    raw: dict[str, Any], meters: list[Meter], deployment: str, provider: str = "azure_openai"
) -> list[RowResult]:
    results: list[RowResult] = []
    for name, spec in (raw.get("models") or {}).items():
        if not isinstance(spec, dict) or spec.get("provider") != provider:
            continue
        dep = str(spec.get("sku", deployment))
        version = spec.get("version")
        resolved = resolve(meters, str(name), dep, None if version is None else str(version))
        for field in FIELDS:
            current = spec.get(field)
            cur = None if current is None else Decimal(str(current))
            retail = resolved.get(field)
            if retail is None:
                if cur is None:
                    continue  # no cached-input meter and none in the book: nothing to say
                results.append(RowResult(str(name), field, cur, None, "unmatched", f"sku {dep}"))
            elif isinstance(retail, list):
                results.append(
                    RowResult(
                        str(name),
                        field,
                        cur,
                        None,
                        "ambiguous",
                        "versions " + ", ".join(retail) + "; set `version:` on the row",
                    )
                )
            elif cur is None:
                results.append(RowResult(str(name), field, None, retail, "missing", f"sku {dep}"))
            elif cur == retail:
                results.append(RowResult(str(name), field, cur, retail, "ok", f"sku {dep}"))
            else:
                results.append(RowResult(str(name), field, cur, retail, "differs", f"sku {dep}"))
    return results


def apply(text: str, results: list[RowResult], today: str | None = None) -> tuple[str, int]:
    """Rewrite differing or missing numbers in the YAML text in place, keeping comments and
    the row layout; bump ``version`` when anything changed. Returns (text, changes)."""
    changes = 0
    for r in results:
        if r.model == BOOK_CURRENCY_ROW and r.status == "differs":
            code = r.note.split("->")[-1].strip()
            text, n = re.subn(r"^(currency:\s*)\S+", rf"\g<1>{code}", text, count=1, flags=re.M)
            changes += n
            continue
        if r.status not in ("differs", "missing") or r.retail is None:
            continue
        row = re.compile(rf'^(\s*"?{re.escape(r.model)}"?:\s*\{{)(.*)(\}}\s*)$', re.M)
        m = row.search(text)
        if m is None:
            continue
        inner = m.group(2)
        value = money(r.retail)
        if r.status == "differs":
            inner, n = re.subn(rf"(\b{r.field}:\s*)[0-9.]+", rf"\g<1>{value}", inner, count=1)
        else:
            inner, n = re.subn(r"(\boutput:\s*[0-9.]+)", rf"\1, {r.field}: {value}", inner, count=1)
        if n:
            text = text[: m.start(2)] + inner + text[m.end(2) :]
            changes += 1
    if changes:
        stamp = today or datetime.now(UTC).date().isoformat()
        text = re.sub(r'^(version:\s*)"?[^"\n]*"?', rf'\g<1>"{stamp}"', text, count=1, flags=re.M)
    return text, changes


def format_table(results: list[RowResult]) -> str:
    lines = [f"{'model':16} {'field':13} {'book':>10} {'retail':>10}  status"]
    for r in results:
        cur = "-" if r.current is None else money(r.current)
        ret = "-" if r.retail is None else money(r.retail)
        note = f"  ({r.note})" if r.note else ""
        lines.append(f"{r.model:16} {r.field:13} {cur:>10} {ret:>10}  {r.status}{note}")
    return "\n".join(lines)


BOOK_CURRENCY_ROW = "(price book)"


def sync(
    path: Path,
    region: str,
    deployment: str = "global",
    write: bool = False,
    items: list[dict[str, Any]] | None = None,
    currency: str = "USD",
) -> tuple[list[RowResult], int]:
    """Compare ``path`` with the retail prices of ``region`` in ``currency``; with ``write``
    update it (numbers, and the book's ``currency`` line when it differs). Returns the
    per-field results and the number of rewritten values."""
    if deployment not in DEPLOYMENTS:
        raise ValueError(f"deployment must be one of {DEPLOYMENTS}")
    currency = currency.upper()
    items = fetch_retail_items(region, currency) if items is None else items
    meters = [m for m in (parse_meter(i) for i in items) if m is not None]
    text = path.read_text(encoding="utf-8")
    raw: Any = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    book_currency = str(raw.get("currency", "USD")).upper()
    results = [
        RowResult(
            BOOK_CURRENCY_ROW,
            "currency",
            None,
            None,
            "ok" if book_currency == currency else "differs",
            f"{book_currency} -> {currency}",
        )
    ]
    results += compare(raw, meters, deployment)
    changes = 0
    if write:
        text, changes = apply(text, results)
        if changes:
            path.write_text(text, encoding="utf-8")
    return results, changes
