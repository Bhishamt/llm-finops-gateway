from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from llm_finops_gateway.ledger import CostEntry, Ledger
from llm_finops_gateway.pricing import PriceBook
from llm_finops_gateway.reconcile import fetch_cost_management, format_report, reconcile

RESOURCE = (
    "/subscriptions/36a028fc-0000-0000-0000-000000000000/resourceGroups/rg/providers/"
    "Microsoft.CognitiveServices/accounts/aoai"
)
QUERY_URL = (
    "https://management.azure.com/subscriptions/36a028fc-0000-0000-0000-000000000000/"
    "providers/Microsoft.CostManagement/query"
)


def _q1() -> dict[str, object]:
    cols = ["Cost", "UsageQuantity", "UsageDate", "Meter", "UnitOfMeasure", "Currency"]
    rows = [
        [0.0052, 12.263, 20261030, "gpt 4.1 mini Inp regnl Tokens", "1K", "EUR"],
        [0.0570, 33.462, 20261030, "gpt 4.1 mini Outp regnl Tokens", "1K", "EUR"],
        [0.0020, 5.634, 20261030, "gpt 4.1 mini Inp glbl Tokens", "1K", "EUR"],
        [0.0184, 13.07, 20261030, "gpt 4.1 mini Outp glbl Tokens", "1K", "EUR"],
        [0.0023, 0.00129, 20261030, "GPT 5 Mini outpt Glbl 1M Tokens", "1M", "EUR"],
        [0.0001, 0.5, 20261030, "gpt 4.1 mini Batch Outp glbl Tokens", "1K", "EUR"],  # ignored
    ]
    return {"properties": {"columns": [{"name": c} for c in cols], "rows": rows}}


def _q2() -> dict[str, object]:
    cols = ["CostUSD", "UsageDate", "Meter", "Currency"]
    rows = [
        [0.005935, 20261030, "gpt 4.1 mini Inp regnl Tokens", "EUR"],
        [0.064782, 20261030, "gpt 4.1 mini Outp regnl Tokens", "EUR"],
        [0.002254, 20261030, "gpt 4.1 mini Inp glbl Tokens", "EUR"],
        [0.020912, 20261030, "gpt 4.1 mini Outp glbl Tokens", "EUR"],
        [0.00258, 20261030, "GPT 5 Mini outpt Glbl 1M Tokens", "EUR"],
    ]
    return {"properties": {"columns": [{"name": c} for c in cols], "rows": rows}}


@respx.mock
def test_fetch_classifies_meters_and_converts_units() -> None:
    route = respx.post(QUERY_URL).mock(
        side_effect=[httpx.Response(200, json=_q1()), httpx.Response(200, json=_q2())]
    )
    meters = fetch_cost_management(RESOURCE, date(2026, 10, 30), token="tok")
    assert route.call_count == 2
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok"
    body = route.calls[0].request.content.decode()
    assert '"from":"2026-10-30T00:00:00Z"' in body and RESOURCE in body
    by = {m.meter: m for m in meters}
    assert "gpt 4.1 mini Batch Outp glbl Tokens" not in by  # batch meters are not chat prices
    assert by["gpt 4.1 mini Inp regnl Tokens"].tokens == 12263
    assert by["gpt 4.1 mini Inp regnl Tokens"].deployment == "regional"
    assert by["GPT 5 Mini outpt Glbl 1M Tokens"].tokens == 1290
    assert by["GPT 5 Mini outpt Glbl 1M Tokens"].cost_usd == Decimal("0.00258")
    assert by["gpt 4.1 mini Outp glbl Tokens"].currency == "EUR"


def _book(tmp_path: Path) -> PriceBook:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: t\ncurrency: USD\nmodels:\n"
        "  gpt-4.1-mini: {input: 0.40, output: 1.60, provider: azure_openai}\n"
        "  gpt-5-mini: {input: 0.25, output: 2.00, provider: azure_openai}\n"
    )
    return PriceBook.from_yaml(path)


def _seed(ledger: Ledger) -> None:
    day = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    for i, (model, inp, out, cost) in enumerate(
        [("gpt-4.1-mini", 17800, 46500, "0.0815"), ("gpt-5-mini", 64, 710, "0.001436")]
    ):
        ledger.record(
            CostEntry(
                request_id=f"r{i}",
                team="t",
                project="p",
                use_case="u",
                provider="azure_openai",
                model=model,
                input_tokens=inp,
                output_tokens=out,
                cost_usd=Decimal(cost),
                price_book_version="t",
                ts=day,
            )
        )
    # a row on another day must not count
    ledger.record(
        CostEntry(
            request_id="other-day",
            team="t",
            project="p",
            use_case="u",
            provider="azure_openai",
            model="gpt-4.1-mini",
            input_tokens=999,
            output_tokens=999,
            cost_usd=Decimal("1"),
            price_book_version="t",
            ts=datetime(2026, 10, 31, 1, 0, tzinfo=UTC),
        )
    )


@respx.mock
def test_reconcile_matches_models_and_reports_deltas(tmp_path: Path, ledger: Ledger) -> None:
    respx.post(QUERY_URL).mock(
        side_effect=[httpx.Response(200, json=_q1()), httpx.Response(200, json=_q2())]
    )
    _seed(ledger)
    meters = fetch_cost_management(RESOURCE, date(2026, 10, 30), token="tok")
    report = reconcile(ledger, _book(tmp_path), meters, date(2026, 10, 30))
    by = {line.model: line for line in report.lines}
    g = by["gpt-4.1-mini"]
    assert (g.ledger_input, g.ledger_output) == (17800, 46500)
    assert (g.azure_input, g.azure_output) == (12263 + 5634, 33462 + 13070)
    assert g.deployments == ("global", "regional")
    assert g.azure_cost_usd == Decimal("0.093883")
    assert by["gpt-5-mini"].azure_output == 1290 and by["gpt-5-mini"].azure_input == 0
    assert report.unmatched == []
    text = format_report(report)
    assert "+33 input" in text and "+612 output" in text
    assert "non-global SKU" in text and "gpt-4.1-mini" in text
    assert "cost USD: Azure 0.096463 vs ledger 0.082936" in text


@respx.mock
def test_cli_ledger_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from llm_finops_gateway.cli import main

    respx.post(QUERY_URL).mock(
        side_effect=[httpx.Response(200, json=_q1()), httpx.Response(200, json=_q2())]
    )
    db = tmp_path / "ledger.db"
    _seed(Ledger(f"sqlite:///{db}"))
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(
        "version: t\ncurrency: USD\nmodels:\n  gpt-4.1-mini: {input: 0.40, output: 1.60}\n"
        "  gpt-5-mini: {input: 0.25, output: 2.00}\n"
    )
    monkeypatch.setenv("LFG_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("LFG_PRICING_PATH", str(pricing))
    main(["ledger", "reconcile", "--resource", RESOURCE, "--day", "2026-10-30", "--token", "tok"])
    out = capsys.readouterr().out
    assert "2026-10-30 (UTC): ledger vs Azure Cost Management" in out
    assert "gpt-4.1-mini" in out and "+33 input" in out
