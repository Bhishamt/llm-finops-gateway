import logging
import os
from pathlib import Path

import pytest

from llm_finops_gateway.pricing import PriceBook
from llm_finops_gateway.reload import WatchedFile

BOOK_V1 = 'version: "v1"\ncurrency: USD\nmodels:\n  m: {input: 1, output: 2}\n'
BOOK_V2 = (
    'version: "v2"\ncurrency: USD\nmodels:\n  m: {input: 3, output: 4}\n'
    "  n: {input: 1, output: 1}\n"
)


def _write(path: Path, text: str, when: int) -> None:
    path.write_text(text)
    os.utime(path, ns=(when * 10**9, when * 10**9))  # an explicit, distinct mtime


@pytest.fixture
def book_file(tmp_path: Path) -> Path:
    p = tmp_path / "pricing.yaml"
    _write(p, BOOK_V1, 1_700_000_000)
    return p


def _watch(path: Path) -> WatchedFile[PriceBook]:
    return WatchedFile(path, PriceBook.from_yaml, lambda b: f"version {b.version}")


def test_unchanged_file_is_not_reloaded(book_file: Path) -> None:
    w = _watch(book_file)
    first = w.get()
    assert w.get() is first and first.version == "v1"


def test_changed_file_is_reloaded_on_next_get(book_file: Path) -> None:
    w = _watch(book_file)
    assert w.get().version == "v1"
    _write(book_file, BOOK_V2, 1_700_000_001)
    book = w.get()
    assert book.version == "v2" and book.has("n")
    assert w.value is book


def test_broken_edit_keeps_the_previous_value(
    book_file: Path, caplog: pytest.LogCaptureFixture
) -> None:
    w = _watch(book_file)
    w.get()
    _write(book_file, "version: v3\ncurrency: usd-not-a-code\nmodels: {}\n", 1_700_000_002)
    with caplog.at_level(logging.ERROR):
        assert w.get().version == "v1"
    assert "failed to load" in caplog.text
    # not retried until the file changes again, then the fix is picked up
    assert w.get().version == "v1"
    _write(book_file, BOOK_V2, 1_700_000_003)
    assert w.get().version == "v2"


def test_missing_file_keeps_the_previous_value(book_file: Path) -> None:
    w = _watch(book_file)
    book_file.unlink()
    assert w.get().version == "v1"


# ---- through the gateway and the plugin ---------------------------------------------------

from collections.abc import Iterator  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from llm_finops_gateway.auth import KeyRing  # noqa: E402
from llm_finops_gateway.config import ProviderKind, Settings  # noqa: E402
from llm_finops_gateway.ledger import Ledger  # noqa: E402
from llm_finops_gateway.main import create_app  # noqa: E402
from tests.conftest import FakeProvider  # noqa: E402

PRICING_A = 'version: "pa"\ncurrency: USD\nmodels:\n  priced: {input: 2.5, output: 10}\n'
PRICING_B = (
    'version: "pb"\ncurrency: USD\nmodels:\n  priced: {input: 2.5, output: 10}\n'
    "  newcomer: {input: 1, output: 1}\n"
)
POLICY_A = 'version: "pola"\ndefaults:\n  allowed_models: ["*"]\n'
POLICY_B = 'version: "polb"\ndefaults:\n  allowed_models: ["newcomer"]\n'


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    pricing, policies = tmp_path / "pricing.yaml", tmp_path / "policies.yaml"
    _write(pricing, PRICING_A, 1_700_000_000)
    _write(policies, POLICY_A, 1_700_000_000)
    return pricing, policies


@pytest.fixture
def rclient(files: tuple[Path, Path]) -> Iterator[TestClient]:
    pricing, policies = files
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="headers",
        pricing_path=pricing,
        policies_path=policies,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        keyring=KeyRing.empty(),
        ledger=Ledger("sqlite:///:memory:"),
        provider=FakeProvider(),
    )
    with TestClient(app) as c:
        yield c


HDR = {"X-Team": "platform", "X-Project": "demo"}


def test_gateway_picks_up_edited_files_without_restart(
    rclient: TestClient, files: tuple[Path, Path]
) -> None:
    pricing, policies = files
    body = {"model": "priced", "messages": [{"role": "user", "content": "hi"}]}
    r = rclient.post("/v1/chat/completions", json=body, headers=HDR)
    assert r.status_code == 200 and r.json()["x_finops"]["price_book_version"] == "pa"
    assert rclient.get("/healthz").json()["policies"] == "pola"
    assert (
        rclient.post(
            "/v1/chat/completions", json={**body, "model": "newcomer"}, headers=HDR
        ).status_code
        == 422
    )

    _write(pricing, PRICING_B, 1_700_000_001)
    _write(policies, POLICY_B, 1_700_000_001)
    r = rclient.post("/v1/chat/completions", json={**body, "model": "newcomer"}, headers=HDR)
    assert r.status_code == 200 and r.json()["x_finops"]["price_book_version"] == "pb"
    r = rclient.post("/v1/chat/completions", json=body, headers=HDR)
    assert r.status_code == 403 and r.json()["detail"]["policy_version"] == "polb"
    health = rclient.get("/healthz").json()
    assert health["price_book"] == "pb" and health["policies"] == "polb"
    assert "newcomer" in [m["id"] for m in rclient.get("/v1/models").json()["data"]]
    # the reports read the version in service as well
    budgets = rclient.get("/v1/finops/budgets").json()
    assert budgets["policy_version"] == "polb"
    assert budgets["teams"][0]["allowed_models"] == ["newcomer"]
    page = rclient.get("/finops").text
    assert "polb" in page and "newcomer" in page and "pola" not in page


async def test_plugin_picks_up_edited_policies(files: tuple[Path, Path]) -> None:
    pytest.importorskip("litellm", reason="[litellm] extra not installed")
    from types import SimpleNamespace

    from fastapi import HTTPException

    from llm_finops_gateway.litellm_plugin import FinOpsPlugin

    pricing, policies = files
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        pricing_path=pricing,
        policies_path=policies,
        database_url="sqlite:///:memory:",
    )
    plugin = FinOpsPlugin(settings)
    key = SimpleNamespace(team_alias="platform", key_alias="k")
    data = {
        "model": "priced",
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {},
        "proxy_server_request": {"headers": {"x-project": "demo"}},
    }
    assert await plugin.async_pre_call_hook(key, None, dict(data), "acompletion") is not None
    _write(policies, POLICY_B, 1_700_000_001)
    with pytest.raises(HTTPException) as exc:
        await plugin.async_pre_call_hook(key, None, dict(data), "acompletion")
    assert exc.value.status_code == 403
