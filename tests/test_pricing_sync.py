from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from llm_finops_gateway.cli import main
from llm_finops_gateway.pricing_sync import (
    RETAIL_URL,
    Meter,
    apply,
    compare,
    fetch_retail_items,
    parse_meter,
    resolve,
    sync,
)


def item(
    meter: str, price: float, unit: str = "1K", product: str = "Azure OpenAI"
) -> dict[str, Any]:
    return {
        "type": "Consumption",
        "serviceName": "Foundry Models",
        "productName": product,
        "meterName": meter,
        "unitOfMeasure": unit,
        "retailPrice": price,
    }


# A slice of the real Sweden Central list (2026-10-30), one of each spelling.
ITEMS = [
    item("gpt 4.1 mini Inp glbl Tokens", 0.0004),
    item("gpt 4.1 mini Outp glbl Tokens", 0.0016),
    item("gpt 4.1 mini cached Inp glbl Tokens", 0.0001),
    item("gpt 4.1 mini Inp regnl Tokens", 0.000484),
    item("gpt 4.1 mini Outp regnl Tokens", 0.001936),
    item("gpt 4.1 mini Inp Data Zone Tokens", 0.00044),
    item("gpt-4o-mini-0718-Inp-glbl Tokens", 0.00015),
    item("gpt 4o mini 0718 cached Inp glbl Tokens", 0.000075),
    item("gpt-4o-mini-0718-Outp-glbl Tokens", 0.0006),
    item("gpt 4o 0513 Input global Tokens", 0.005),
    item("gpt 4o 1120 Inp glbl Tokens", 0.0025),
    item("gpt-4o-0806-Inp-glbl Tokens", 0.0025),
    item("GPT 5 Mini Inpt Glbl 1M Tokens", 0.25, unit="1M", product="Azure OpenAI GPT5"),
    item("GPT 5 Mini outpt Glbl 1M Tokens", 2.0, unit="1M", product="Azure OpenAI GPT5"),
    item("GPT 5 Mini cchd Inpt Glbl 1M Tokens", 0.025, unit="1M", product="Azure OpenAI GPT5"),
    # must be ignored
    item("gpt 4.1 mini Batch Outp glbl Tokens", 0.0008),
    item("gpt-4.1-mini-ft input global Tokens", 0.0004),
    item("gpt-4o-mini-tts-txt-inp-glbl Tokens", 0.0006),
    item("gpt-4.1-mini-ft hosting regional Unit", 1.7, unit="1 Hour"),
]


@pytest.mark.parametrize(
    ("meter", "expected"),
    [
        ("gpt 4.1 mini Inp glbl Tokens", (("gpt", "4.1", "mini"), None, "input", "global")),
        (
            "gpt-4o-mini-0718-Outp-regnl Tokens",
            (("gpt", "4o", "mini"), "0718", "output", "regional"),
        ),
        (
            "GPT 5 Mini cchd Inpt Glbl 1M Tokens",
            (("gpt", "5", "mini"), None, "cached_input", "global"),
        ),
        ("gpt 4o 0513 Input Data Zone Tokens", (("gpt", "4o"), "0513", "input", "datazone")),
    ],
)
def test_parse_meter_handles_every_spelling(
    meter: str, expected: tuple[tuple[str, ...], str | None, str, str]
) -> None:
    parsed = parse_meter(item(meter, 0.001))
    assert parsed is not None
    assert (parsed.model_tokens, parsed.version, parsed.field, parsed.deployment) == expected


def test_parse_meter_converts_per_1k_to_per_million_and_skips_other_meters() -> None:
    parsed = parse_meter(item("gpt 4.1 mini Inp regnl Tokens", 0.000484))
    assert parsed is not None and parsed.price_per_million == Decimal("0.484")
    assert parse_meter(item("gpt 4.1 mini Batch Outp glbl Tokens", 0.0008)) is None
    assert parse_meter(item("gpt-4.1-mini-ft input global Tokens", 0.0004)) is None
    assert parse_meter(item("gpt-4.1-mini-ft hosting regional Unit", 1.7, unit="1 Hour")) is None
    assert (
        parse_meter(item("FW DS-V4.1-Flash Gl Opt Tokens", 0.0015, product="Azure Fireworks"))
        is None
    )


def _meters() -> list[Meter]:
    return [m for m in (parse_meter(i) for i in ITEMS) if m is not None]


def test_resolve_prices_by_deployment_type() -> None:
    assert resolve(_meters(), "gpt-4.1-mini", "global", None) == {
        "input": Decimal("0.4"),
        "output": Decimal("1.6"),
        "cached_input": Decimal("0.1"),
    }
    assert resolve(_meters(), "gpt-4.1-mini", "regional", None) == {
        "input": Decimal("0.484"),
        "output": Decimal("1.936"),
    }
    assert resolve(_meters(), "gpt-5-mini", "global", None)["output"] == Decimal("2")


def test_resolve_is_ambiguous_across_model_versions_until_the_row_names_one() -> None:
    assert resolve(_meters(), "gpt-4o", "global", None)["input"] == ["0513", "0806", "1120"]
    assert resolve(_meters(), "gpt-4o", "global", "2024-11-20")["input"] == Decimal("2.5")
    # two spellings of the same meter with the same price are not ambiguous
    assert resolve(_meters(), "gpt-4o-mini", "global", None)["input"] == Decimal("0.15")


BOOK = """\
# comment stays
version: "2026-11-02"
currency: USD
models:
  gpt-4.1-mini: { input: 0.40, output: 1.60, cached_input: 0.10, provider: azure_openai }
  gpt-4o-mini: { input: 0.15, output: 0.60, provider: azure_openai }
  gpt-4o: { input: 2.50, output: 10.00, provider: azure_openai }
  gpt-5-mini: { input: 0.30, output: 2.00, cached_input: 0.025, provider: azure_openai }
  "llama3.2:3b": { input: 0.00, output: 0.00, provider: ollama }
"""


def test_compare_reports_ok_differs_missing_and_ambiguous(tmp_path: Path) -> None:
    import yaml

    results = compare(yaml.safe_load(BOOK), _meters(), "global")
    by = {(r.model, r.field): r.status for r in results}
    assert by[("gpt-4.1-mini", "input")] == "ok"
    assert by[("gpt-4o-mini", "cached_input")] == "missing"
    assert by[("gpt-4o", "input")] == "ambiguous"
    assert by[("gpt-5-mini", "input")] == "differs"
    assert by[("gpt-5-mini", "output")] == "ok"
    assert ("llama3.2:3b", "input") not in by  # not an Azure row


def test_sync_write_rewrites_numbers_in_place_and_bumps_version(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(BOOK)
    results, changes = sync(path, "swedencentral", "global", write=True, items=ITEMS)
    assert changes == 2  # gpt-5-mini input, gpt-4o-mini cached_input
    text = path.read_text()
    assert text.startswith("# comment stays\n")
    assert 'version: "20' in text and 'version: "2026-11-02"' not in text
    assert "gpt-5-mini: { input: 0.25, output: 2.00, cached_input: 0.025" in text
    assert "gpt-4o-mini: { input: 0.15, output: 0.60, cached_input: 0.075, provider" in text
    assert "gpt-4o: { input: 2.50, output: 10.00, provider" in text  # ambiguous: untouched
    assert [r.status for r in results if r.model == "gpt-4.1-mini"] == ["ok", "ok", "ok"]


def test_apply_without_changes_keeps_text_and_version() -> None:
    text, changes = apply(BOOK, [], today="2030-01-01")
    assert (text, changes) == (BOOK, 0)


def test_row_sku_overrides_the_default_deployment() -> None:
    import yaml

    book = BOOK.replace(
        "cached_input: 0.10, provider: azure_openai }",
        "cached_input: 0.10, provider: azure_openai, sku: regional }",
    )
    results = compare(yaml.safe_load(book), _meters(), "global")
    row = {r.field: r for r in results if r.model == "gpt-4.1-mini"}
    assert row["input"].status == "differs" and row["input"].retail == Decimal("0.484")
    assert row["cached_input"].status == "unmatched"  # no regional cached meter in the sample


@respx.mock
def test_fetch_follows_pagination() -> None:
    page2 = "https://prices.azure.com/api/retail/prices?$skip=1000"
    # one route serves both pages (same path, different query): page 1 then page 2
    route = respx.get(RETAIL_URL).mock(
        side_effect=[
            httpx.Response(200, json={"Items": ITEMS[:2], "NextPageLink": page2}),
            httpx.Response(200, json={"Items": ITEMS[2:4]}),
        ]
    )
    items = fetch_retail_items("swedencentral")
    assert route.call_count == 2
    assert str(respx.calls[1].request.url) == page2
    assert [i["meterName"] for i in items] == [i["meterName"] for i in ITEMS[:4]]
    first = respx.calls[0].request
    assert "armRegionName eq 'swedencentral'" in first.url.params["$filter"]


@respx.mock
def test_cli_prints_table_and_exits_nonzero_on_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get(RETAIL_URL).mock(return_value=httpx.Response(200, json={"Items": ITEMS}))
    path = tmp_path / "pricing.yaml"
    path.write_text(BOOK)
    with pytest.raises(SystemExit) as exc:
        main(["pricing", "sync", "--region", "swedencentral", "--path", str(path)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "gpt-5-mini" in out and "differs" in out and "ambiguous" in out
    # --write rewrites what it can; the ambiguous gpt-4o row still needs a human, so exit 1
    with pytest.raises(SystemExit) as exc:
        main(["pricing", "sync", "--region", "swedencentral", "--path", str(path), "--write"])
    assert exc.value.code == 1
    assert "input: 0.25" in path.read_text()
    assert "value(s) rewritten" in capsys.readouterr().out


@respx.mock
def test_fetch_asks_for_the_currency() -> None:
    respx.get(RETAIL_URL).mock(return_value=httpx.Response(200, json={"Items": []}))
    fetch_retail_items("swedencentral", "EUR")
    assert respx.calls.last.request.url.params["currencyCode"] == "'EUR'"


def test_currency_mismatch_is_reported_and_written(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(BOOK)
    results, _ = sync(path, "swedencentral", "global", items=ITEMS, currency="eur")
    assert results[0].model == "(price book)" and results[0].status == "differs"
    assert results[0].note == "USD -> EUR"
    _, changes = sync(path, "swedencentral", "global", write=True, items=ITEMS, currency="EUR")
    assert changes >= 1 and "currency: EUR" in path.read_text()
    results, _ = sync(path, "swedencentral", "global", items=ITEMS, currency="EUR")
    assert results[0].status == "ok"
