from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_finops_gateway.auth import KeyRing, generate_key
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import CostEntry, Ledger
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from tests.conftest import FakeProvider

RAW_PLATFORM, HASH_PLATFORM = generate_key()
RAW_FINOPS, HASH_FINOPS = generate_key()


def _seed(ledger: Ledger) -> None:
    for i, (team, cost) in enumerate((("platform", "0.5"), ("research", "0.25"))):
        ledger.record(
            CostEntry(
                request_id=f"seed-{i}",
                team=team,
                project="p",
                use_case="u",
                provider="fake",
                model="priced",
                input_tokens=10,
                output_tokens=5,
                cost_usd=Decimal(cost),
                price_book_version="test-1",
            )
        )


def _app(
    mode: str, pricing_path: Path, price_book: PriceBook, ledger: Ledger, tmp_path: Path
) -> TestClient:
    keys = tmp_path / "keys.yaml"
    keys.write_text(
        "version: t\nkeys:\n"
        f"  - {{id: platform-key, team: platform, sha256: {HASH_PLATFORM}}}\n"
        f"  - {{id: finops-key, team: finops, sha256: {HASH_FINOPS}, role: finops}}\n"
    )
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode=mode,  # type: ignore[arg-type]
        pricing_path=pricing_path,
        keys_path=keys,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=KeyRing.from_yaml(keys),
        ledger=ledger,
        provider=FakeProvider(),
    )
    return TestClient(app)


@pytest.fixture
def open_client(
    pricing_path: Path, price_book: PriceBook, ledger: Ledger, tmp_path: Path
) -> Iterator[TestClient]:
    _seed(ledger)
    with _app("headers", pricing_path, price_book, ledger, tmp_path) as c:
        yield c


@pytest.fixture
def keyed_client(
    pricing_path: Path, price_book: PriceBook, ledger: Ledger, tmp_path: Path
) -> Iterator[TestClient]:
    _seed(ledger)
    with _app("api_key", pricing_path, price_book, ledger, tmp_path) as c:
        yield c


def test_html_report_lists_budgets_and_chargeback(open_client: TestClient) -> None:
    r = open_client.get("/finops")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "platform" in r.text and "research" in r.text and "0.75" in r.text
    assert "all teams" in r.text and "price book test-1" in r.text


def test_csv_summary(open_client: TestClient) -> None:
    r = open_client.get("/v1/finops/summary.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0] == "team,project,model,calls,input_tokens,output_tokens,cost_usd"
    assert "platform,p,priced,1,10,5,0.5" in lines and "research,p,priced,1,10,5,0.25" in lines


def test_team_key_sees_only_its_own_team(keyed_client: TestClient) -> None:
    auth = {"Authorization": f"Bearer {RAW_PLATFORM}"}
    summary = keyed_client.get("/v1/finops/summary", headers=auth).json()
    assert {r["team"] for r in summary["rows"]} == {"platform"}
    assert summary["total_cost_usd"] == "0.5"
    budgets = keyed_client.get("/v1/finops/budgets", headers=auth).json()
    assert [t["team"] for t in budgets["teams"]] == ["platform"]
    other = keyed_client.get("/v1/finops/summary", headers={**auth, "X-Team": "research"})
    assert other.status_code == 403 and other.json()["detail"]["error"] == "team_mismatch"


def test_finops_key_sees_every_team_and_can_filter(keyed_client: TestClient) -> None:
    auth = {"Authorization": f"Bearer {RAW_FINOPS}"}
    summary = keyed_client.get("/v1/finops/summary", headers=auth).json()
    assert {r["team"] for r in summary["rows"]} == {"platform", "research"}
    filtered = keyed_client.get("/v1/finops/summary?team=research", headers=auth).json()
    assert [r["team"] for r in filtered["rows"]] == ["research"]
    page = keyed_client.get("/finops", headers=auth)
    assert page.status_code == 200 and "research" in page.text and "key finops-key" in page.text


def test_reports_need_a_key_in_api_key_mode(keyed_client: TestClient) -> None:
    assert keyed_client.get("/v1/finops/summary").status_code == 401
    assert keyed_client.get("/v1/finops/summary.csv").status_code == 401
    page = keyed_client.get("/finops")
    assert page.status_code == 200 and '<form method="post" action="/finops/login"' in page.text
    assert "platform" not in page.text


def test_login_form_sets_cookie_and_scopes_the_page(keyed_client: TestClient) -> None:
    r = keyed_client.post("/finops/login", data={"key": RAW_PLATFORM}, follow_redirects=False)
    assert r.status_code == 303 and "lfg_report_key" in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower()
    page = keyed_client.get("/finops")
    assert page.status_code == 200
    assert "team <b>platform</b>" in page.text and "research" not in page.text
    bad = keyed_client.post("/finops/login", data={"key": "lfg_wrong"}, follow_redirects=False)
    assert bad.status_code == 200 and "invalid api key" in bad.text
    keyed_client.get("/finops/logout", follow_redirects=False)
    assert '<form method="post"' in keyed_client.get("/finops").text
