import csv
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.focus import COLUMNS, aggregate, focus_csv
from llm_finops_gateway.ledger import CostEntry, Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from tests.conftest import FakeProvider


def _entry(
    ts: datetime,
    team: str,
    project: str,
    model: str,
    cost: str,
    rid: str,
    use_case: str = "faq",
    run_id: str | None = None,
) -> CostEntry:
    return CostEntry(
        request_id=rid,
        team=team,
        project=project,
        use_case=use_case,
        provider="azure_openai",
        model=model,
        input_tokens=100,
        output_tokens=10,
        cached_input_tokens=20,
        cost_usd=Decimal(cost),
        price_book_version="2026-11-02",
        ts=ts,
        run_id=run_id,
    )


@pytest.fixture
def filled(ledger: Ledger) -> Ledger:
    d1 = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    d1b = datetime(2026, 11, 3, 17, 30, tzinfo=UTC)
    d2 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    ledger.record(_entry(d1, "platform", "demo", "gpt-4.1-mini", "0.001", "r1"))
    ledger.record(_entry(d1b, "platform", "demo", "gpt-4.1-mini", "0.002", "r2"))
    ledger.record(_entry(d2, "platform", "demo", "gpt-4.1-mini", "0.004", "r3"))
    ledger.record(_entry(d2, "research", "lab", "gpt-5-mini", "0.008", "r4", use_case="eval"))
    return ledger


def test_rows_are_aggregated_per_day_and_attribution(filled: Ledger) -> None:
    rows = filled.rows(datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC))
    assert [r.request_id for r in rows] == ["r1", "r2", "r3", "r4"]
    agg = aggregate(rows)
    assert len(agg) == 3
    first = agg[0]
    assert first.day == datetime(2026, 11, 3, tzinfo=UTC)
    assert first.calls == 2 and first.cost == Decimal("0.003")
    assert first.input_tokens == 200 and first.cached_input_tokens == 40
    assert agg[2].team == "research" and agg[2].use_case == "eval"


def test_run_id_is_exported_in_tags_and_keeps_runs_apart(ledger: Ledger) -> None:
    d = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    ledger.record(_entry(d, "platform", "demo", "gpt-4.1-mini", "0.001", "r1", run_id="run-a"))
    ledger.record(_entry(d, "platform", "demo", "gpt-4.1-mini", "0.002", "r2", run_id="run-a"))
    ledger.record(_entry(d, "platform", "demo", "gpt-4.1-mini", "0.004", "r3", run_id="run-b"))
    ledger.record(_entry(d, "platform", "demo", "gpt-4.1-mini", "0.008", "r4"))
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    rows = aggregate(ledger.rows(start, end))
    assert [(r.run_id, r.calls) for r in rows] == [("run-a", 2), ("run-b", 1), (None, 1)]
    csv_rows = list(
        csv.DictReader(io.StringIO(focus_csv(ledger.rows(start, end), "USD", start, end)))
    )
    assert [json.loads(r["Tags"]).get("run_id") for r in csv_rows] == ["run-a", "run-b", None]


def test_rows_window_and_filters(filled: Ledger) -> None:
    nov4 = datetime(2026, 11, 4, tzinfo=UTC)
    assert len(filled.rows(nov4, datetime(2026, 11, 5, tzinfo=UTC))) == 2
    assert len(filled.rows(nov4, datetime(2026, 11, 5, tzinfo=UTC), team="research")) == 1
    assert filled.rows(datetime(2026, 11, 1, tzinfo=UTC), nov4, project="lab") == []


def test_focus_csv_has_the_mandatory_columns_and_consistent_costs(filled: Ledger) -> None:
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    text = focus_csv(filled.rows(start, end), "USD", start, end, billing_account_id="acct-1")
    lines = text.splitlines()
    assert lines[0].split(",") == COLUMNS
    assert len(lines) == 4
    import csv
    import io
    import json

    rows = list(csv.DictReader(io.StringIO(text)))
    r = rows[0]
    assert r["BilledCost"] == r["EffectiveCost"] == r["ListCost"] == r["ContractedCost"] == "0.003"
    assert r["BillingCurrency"] == "USD" and r["BillingAccountId"] == "acct-1"
    assert r["ChargePeriodStart"] == "2026-11-03T00:00:00Z"
    assert r["ChargePeriodEnd"] == "2026-11-04T00:00:00Z"
    assert r["BillingPeriodStart"] == "2026-11-01T00:00:00Z"
    assert r["ProviderName"] == "Microsoft" and r["ServiceName"] == "Azure OpenAI"
    assert r["ServiceCategory"] == "AI and Machine Learning"
    assert r["SubAccountId"] == "platform" and r["ResourceId"] == "gpt-4.1-mini"
    assert r["SkuPriceId"] == "gpt-4.1-mini@2026-11-02"
    assert r["ConsumedQuantity"] == "220" and r["ConsumedUnit"] == "Tokens"
    assert json.loads(r["Tags"]) == {"team": "platform", "project": "demo", "use_case": "faq"}
    assert r["x_Calls"] == "2" and r["x_CachedInputTokens"] == "40"
    total = sum(Decimal(x["BilledCost"]) for x in rows)
    assert total == Decimal("0.015")


def test_unknown_provider_is_exported_under_its_own_name() -> None:
    ledger = Ledger("sqlite:///:memory:")
    e = _entry(datetime(2026, 11, 3, tzinfo=UTC), "t", "p", "m", "0.1", "x")
    ledger.record(CostEntry(**{**e.__dict__, "provider": "vllm-lab"}))
    start, end = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 12, 1, tzinfo=UTC)
    text = focus_csv(ledger.rows(start, end), "USD", start, end)
    assert ",vllm-lab,vllm-lab," in text.splitlines()[1]


def test_http_export_is_scoped_and_windowed(
    pricing_path: Path, price_book: PriceBook, filled: Ledger, fake_provider: FakeProvider
) -> None:
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=filled,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        r = c.get("/v1/finops/export.focus.csv?from=2026-11-01&to=2026-12-01")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "llm-finops-focus-20261101-20261201.csv" in r.headers["content-disposition"]
        assert len(r.text.splitlines()) == 4
        r = c.get("/v1/finops/export.focus.csv?from=2026-11-04&to=2026-11-05&team=research")
        assert len(r.text.splitlines()) == 2 and "research" in r.text
        r = c.get("/v1/finops/export.focus.csv?from=2026-11-05&to=2026-11-04")
        assert r.status_code == 422


def test_cli_export_writes_focus_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from llm_finops_gateway.cli import main
    from tests.conftest import PRICING_YAML

    db = tmp_path / "ledger.db"
    ledger = Ledger(f"sqlite:///{db}")
    ledger.record(_entry(datetime(2026, 11, 3, tzinfo=UTC), "platform", "demo", "m", "0.5", "a"))
    (tmp_path / "pricing.yaml").write_text(PRICING_YAML)
    monkeypatch.setenv("LFG_DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("LFG_PRICING_PATH", str(tmp_path / "pricing.yaml"))
    out = tmp_path / "focus.csv"
    main(["ledger", "export", "--from", "2026-11-01", "--to", "2026-12-01", "--out", str(out)])
    lines = out.read_text().splitlines()
    assert lines[0].startswith("BilledCost,") and len(lines) == 2 and lines[1].startswith("0.5,")
    assert "wrote 1 FOCUS rows" in capsys.readouterr().err
    main(["ledger", "export", "--from", "2026-11-01", "--to", "2026-12-01"])
    assert capsys.readouterr().out.startswith("BilledCost,")
