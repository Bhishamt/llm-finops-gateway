"""The report page as a LiteLLM dashboard plugin (ADR-0024)."""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from llm_finops_gateway.auth import KeyRing
from llm_finops_gateway.config import ProviderKind, Settings
from llm_finops_gateway.ledger import Ledger
from llm_finops_gateway.litellm_ui import (
    SESSION_COOKIE,
    derive_key,
    issue_session,
    manifest,
    verify_claim,
    verify_session,
)
from llm_finops_gateway.main import create_app
from llm_finops_gateway.policy import PolicySet
from llm_finops_gateway.pricing import PriceBook
from tests.conftest import FakeProvider

SALT, NAME = "sk-salt-test", "finops"


def litellm_claim(user_role: str = "proxy_admin", plugin: str = NAME, exp_delta: int = 30) -> str:
    """A claim the way LiteLLM issues it (its documented key derivation)."""
    key = base64.urlsafe_b64encode(
        hmac.new(SALT.encode(), plugin.encode(), hashlib.sha256).digest()
    )
    payload = {
        "plugin": plugin,
        "user_id": "user_abc",
        "user_role": user_role,
        "exp": int(time.time()) + exp_delta,
    }
    return Fernet(key).encrypt(json.dumps(payload).encode()).decode()


def test_derived_key_matches_litellms_one_liner() -> None:
    expected = base64.urlsafe_b64encode(
        hmac.new(SALT.encode(), NAME.encode(), hashlib.sha256).digest()
    )
    assert derive_key(SALT, NAME) == expected


def test_claim_round_trip_and_rejections() -> None:
    claim = verify_claim(SALT, NAME, litellm_claim())
    assert claim["user_id"] == "user_abc" and claim["user_role"] == "proxy_admin"
    with pytest.raises(ValueError, match="another plugin"):
        verify_claim(SALT, NAME, litellm_claim(plugin="other") if False else _reaudience())
    with pytest.raises(ValueError, match="tampered"):
        verify_claim(SALT, NAME, litellm_claim()[:-4] + "AAAA")
    with pytest.raises(ValueError, match="tampered"):
        verify_claim("sk-other-salt", NAME, litellm_claim())
    with pytest.raises(ValueError, match="expired"):
        verify_claim(SALT, NAME, litellm_claim(exp_delta=-5))


def _reaudience() -> str:
    """A claim encrypted with this plugin's key but naming another plugin."""
    payload = {
        "plugin": "other",
        "user_id": "u",
        "user_role": "proxy_admin",
        "exp": int(time.time()) + 30,
    }
    return Fernet(derive_key(SALT, NAME)).encrypt(json.dumps(payload).encode()).decode()


def test_session_token_round_trip() -> None:
    token = issue_session(SALT, NAME, "user_abc", "proxy_admin")
    assert verify_session(SALT, NAME, token) == {"user_id": "user_abc", "user_role": "proxy_admin"}
    assert verify_session(SALT, NAME, token[:-2] + "zz") is None
    assert verify_session("sk-other", NAME, token) is None
    assert (
        verify_session(SALT, NAME, None) is None and verify_session(SALT, NAME, "garbage") is None
    )


def test_manifest_shape() -> None:
    m = manifest(NAME, "0.3.0")
    assert m["name"] == NAME and m["nav_items"][0]["path"] == "/" and "reports" in m["capabilities"]


@pytest.fixture
def plugin_client(
    pricing_path: Path,
    price_book: PriceBook,
    ledger: Ledger,
    fake_provider: FakeProvider,
    tmp_path: Path,
) -> Iterator[TestClient]:
    keys_path = tmp_path / "keys.yaml"
    keys_path.write_text("keys: []\n")  # api_key mode with no keys: only a plugin session gets in
    settings = Settings(
        provider=ProviderKind.OLLAMA,
        auth_mode="api_key",
        pricing_path=pricing_path,
        keys_path=keys_path,
        database_url="sqlite:///:memory:",
        litellm_plugin_name=NAME,
        litellm_salt_key=SecretStr(SALT),
    )
    app = create_app(
        settings,
        price_book=price_book,
        policies=PolicySet.empty(),
        keyring=KeyRing.empty(),
        ledger=ledger,
        provider=fake_provider,
    )
    # https: the session cookie is Secure (a third-party frame needs SameSite=None; Secure)
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_dashboard_claim_opens_the_report_for_admins(plugin_client: TestClient) -> None:
    c = plugin_client
    # api_key mode, no key, no session: the HTML page shows its login form, the API refuses
    assert 'action="/finops/login"' in c.get("/finops").text
    assert c.get("/v1/finops/summary").status_code == 401
    assert "litellm-auth" in c.get("/").text
    assert c.get("/api/plugin-manifest").json()["name"] == NAME
    assert c.get("/api/plugin-session").status_code == 401

    r = c.post("/api/plugin-auth", json={"session_claim": litellm_claim()})
    assert r.status_code == 200 and r.json() == {"user_id": "user_abc", "user_role": "proxy_admin"}
    cookie = r.headers["set-cookie"]
    assert (
        SESSION_COOKIE in cookie
        and "HttpOnly" in cookie
        and "SameSite=none" in cookie.replace("None", "none")
    )
    assert c.get("/api/plugin-session").json()["user_id"] == "user_abc"
    page = c.get("/finops")
    assert page.status_code == 200 and 'action="/finops/login"' not in page.text
    assert c.get("/v1/finops/summary").status_code == 200


def test_non_admin_claim_is_refused(plugin_client: TestClient) -> None:
    r = plugin_client.post(
        "/api/plugin-auth", json={"session_claim": litellm_claim("internal_user")}
    )
    assert r.status_code == 403
    assert plugin_client.get("/v1/finops/summary").status_code == 401
    assert plugin_client.post("/api/plugin-auth", json={}).status_code == 400
    assert plugin_client.post("/api/plugin-auth", json={"session_claim": "nope"}).status_code == 401


def test_plugin_routes_absent_without_configuration(
    pricing_path: Path, price_book: PriceBook, ledger: Ledger, fake_provider: FakeProvider
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
        ledger=ledger,
        provider=fake_provider,
    )
    with TestClient(app) as c:
        assert c.get("/api/plugin-manifest").status_code == 404
        assert c.get("/").status_code == 404
