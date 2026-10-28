from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_finops_gateway.auth import KeyRing, KeyRingError, generate_key, hash_key
from llm_finops_gateway.cli import main as cli_main
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import Ledger
from llm_finops_gateway.main import create_app, load_keyring
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from tests.conftest import FakeProvider

RAW_PLATFORM, HASH_PLATFORM = generate_key()
RAW_SUPPORT, HASH_SUPPORT = generate_key()
RAW_OLD, HASH_OLD = generate_key()
RAW_OFF, HASH_OFF = generate_key()

KEYS_YAML = f"""\
version: test-keys
keys:
  - id: platform-copilot
    team: platform
    sha256: {HASH_PLATFORM}
  - id: support-triage
    team: customer-support
    sha256: {HASH_SUPPORT}
    projects: [triage]
  - id: old
    team: platform
    sha256: {HASH_OLD}
    expires: 2000-01-01
  - id: switched-off
    team: platform
    sha256: {HASH_OFF}
    disabled: true
"""


@pytest.fixture
def keys_path(tmp_path: Path) -> Path:
    p = tmp_path / "keys.yaml"
    p.write_text(KEYS_YAML)
    return p


@pytest.fixture
def keyring(keys_path: Path) -> KeyRing:
    return KeyRing.from_yaml(keys_path)


# ---- KeyRing ----------------------------------------------------------------------------


def test_generate_and_hash_round_trip() -> None:
    raw, digest = generate_key()
    assert raw.startswith("lfg_") and len(raw) > 30
    assert hash_key(raw) == digest and len(digest) == 64


def test_lookup_finds_by_hash_only(keyring: KeyRing) -> None:
    key = keyring.lookup(RAW_PLATFORM)
    assert key is not None and key.id == "platform-copilot" and key.team == "platform"
    assert keyring.lookup("lfg_nope") is None
    assert keyring.lookup(HASH_PLATFORM) is None  # sending the hash itself must not work


def test_key_flags(keyring: KeyRing) -> None:
    old = keyring.lookup(RAW_OLD)
    assert old is not None and old.is_expired(date(2026, 1, 1))
    off = keyring.lookup(RAW_OFF)
    assert off is not None and off.disabled
    support = keyring.lookup(RAW_SUPPORT)
    assert support is not None
    assert support.allows_project("triage") and not support.allows_project("other")


@pytest.mark.parametrize(
    "content",
    [
        "keys:\n"
        f"  - {{id: a, team: t, sha256: {HASH_PLATFORM}}}\n"
        f"  - {{id: b, team: t, sha256: {HASH_PLATFORM}}}\n",  # duplicate hash
        "keys:\n  - id: a\n    team: t\n    sha256: nothex\n",
        "keys:\n  - id: a\n    team: t\n    sha256: " + "0" * 64 + "\n    extra: 1\n",
        "- list\n",
    ],
)
def test_malformed_key_file_rejected(tmp_path: Path, content: str) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(content)
    with pytest.raises(KeyRingError):
        KeyRing.from_yaml(p)


def test_load_keyring_requires_file_in_api_key_mode(tmp_path: Path) -> None:
    settings = Settings(
        provider=ProviderKind.OLLAMA, auth_mode="api_key", keys_path=tmp_path / "missing.yaml"
    )
    with pytest.raises(RuntimeError, match="LFG_AUTH_MODE=headers"):
        load_keyring(settings)


def test_cli_keys_new_prints_key_and_yaml(capsys: pytest.CaptureFixture[str]) -> None:
    cli_main(
        [
            "keys",
            "new",
            "--team",
            "platform",
            "--id",
            "demo",
            "--project",
            "x",
            "--expires",
            "2027-01-01",
        ]
    )
    out = capsys.readouterr().out
    raw = next(line for line in out.splitlines() if line.startswith("lfg_"))
    assert f"sha256: {hash_key(raw)}" in out
    assert "team: platform" in out and "projects: [x]" in out and "expires: 2027-01-01" in out


# ---- HTTP in api_key mode -----------------------------------------------------------------


@pytest.fixture
def kclient(
    pricing_path: Path,
    price_book: PriceBook,
    keyring: KeyRing,
    ledger: Ledger,
    fake_provider: FakeProvider,
) -> Iterator[TestClient]:
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="api_key",
        pricing_path=pricing_path,
        database_url="sqlite:///:memory:",
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=keyring,
        ledger=ledger,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        yield c


BODY = {"model": "priced", "messages": [{"role": "user", "content": "hi"}]}


def _auth(raw: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}", "X-Project": "demo", **extra}


def test_missing_key_401(kclient: TestClient, fake_provider: FakeProvider) -> None:
    r = kclient.post("/v1/chat/completions", json=BODY, headers={"X-Project": "demo"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "missing_api_key"
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert fake_provider.calls == []


def test_x_team_alone_is_not_enough_in_api_key_mode(kclient: TestClient) -> None:
    r = kclient.post(
        "/v1/chat/completions", json=BODY, headers={"X-Team": "platform", "X-Project": "demo"}
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    ("raw", "error"),
    [("lfg_nope", "invalid_api_key"), (RAW_OLD, "api_key_expired"), (RAW_OFF, "api_key_disabled")],
)
def test_bad_keys_401(kclient: TestClient, raw: str, error: str) -> None:
    r = kclient.post("/v1/chat/completions", json=BODY, headers=_auth(raw))
    assert r.status_code == 401 and r.json()["detail"]["error"] == error


def test_team_comes_from_key(kclient: TestClient, ledger: Ledger) -> None:
    r = kclient.post("/v1/chat/completions", json=BODY, headers=_auth(RAW_PLATFORM))
    assert r.status_code == 200
    x = r.json()["x_finops"]
    assert x["team"] == "platform" and x["key_id"] == "platform-copilot"
    assert ledger.summary()[0].team == "platform"


def test_matching_x_team_is_fine_mismatch_is_403(kclient: TestClient) -> None:
    ok = kclient.post(
        "/v1/chat/completions", json=BODY, headers=_auth(RAW_PLATFORM, **{"X-Team": "platform"})
    )
    assert ok.status_code == 200
    bad = kclient.post(
        "/v1/chat/completions", json=BODY, headers=_auth(RAW_PLATFORM, **{"X-Team": "research"})
    )
    assert bad.status_code == 403 and bad.json()["detail"]["error"] == "team_mismatch"


def test_project_restriction(kclient: TestClient) -> None:
    bad = kclient.post("/v1/chat/completions", json=BODY, headers=_auth(RAW_SUPPORT))
    assert bad.status_code == 403 and bad.json()["detail"]["error"] == "project_not_allowed"
    ok = kclient.post(
        "/v1/chat/completions",
        json=BODY,
        headers={"Authorization": f"Bearer {RAW_SUPPORT}", "X-Project": "triage"},
    )
    assert ok.status_code == 200 and ok.json()["x_finops"]["team"] == "customer-support"


def test_project_still_required_with_key(kclient: TestClient) -> None:
    r = kclient.post(
        "/v1/chat/completions", json=BODY, headers={"Authorization": f"Bearer {RAW_PLATFORM}"}
    )
    assert r.status_code == 400 and r.json()["detail"]["missing_headers"] == ["X-Project"]


def test_openai_sdk_style_call(kclient: TestClient) -> None:
    """What the OpenAI SDK sends: Bearer api_key plus default_headers."""
    r = kclient.post(
        "/v1/chat/completions",
        json=BODY,
        headers={
            "Authorization": f"Bearer {RAW_PLATFORM}",
            "X-Project": "copilot",
            "X-Use-Case": "code-review",
            "User-Agent": "OpenAI/Python 1.50.0",
        },
    )
    assert r.status_code == 200
    assert r.json()["x_finops"]["use_case"] == "code-review"
