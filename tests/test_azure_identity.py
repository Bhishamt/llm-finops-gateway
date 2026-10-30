from __future__ import annotations

import time
from dataclasses import dataclass

from llm_finops_gateway.providers.azure_identity import (
    COGNITIVE_SERVICES_SCOPE,
    EntraTokenProvider,
)


@dataclass
class AccessToken:
    token: str
    expires_on: float


class FakeCredential:
    """Stands in for azure.identity.DefaultAzureCredential (sync, called in a thread)."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self.scopes: list[str] = []
        self.closed = False

    def get_token(self, scope: str) -> AccessToken:
        self.scopes.append(scope)
        return AccessToken(f"t{len(self.scopes)}", time.time() + self.ttl)

    def close(self) -> None:
        self.closed = True


async def test_token_is_cached_until_close_to_expiry() -> None:
    cred = FakeCredential(ttl=3600)
    provider = EntraTokenProvider(credential=cred)
    assert await provider.token() == "t1"
    assert await provider.token() == "t1"
    assert cred.scopes == [COGNITIVE_SERVICES_SCOPE]
    await provider.aclose()
    assert cred.closed


async def test_token_is_refreshed_inside_the_expiry_margin() -> None:
    cred = FakeCredential(ttl=60)  # inside the 300 s refresh margin from the start
    provider = EntraTokenProvider(credential=cred)
    assert await provider.token() == "t1"
    assert await provider.token() == "t2"
