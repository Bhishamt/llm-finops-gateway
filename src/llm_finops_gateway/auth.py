"""Team-bound API keys (ADR-0006).

A key identifies a team, not a person. The gateway stores only the SHA-256 of each key in
``config/keys.yaml`` (safe to commit); the raw key is shown once by
``llm-finops-gateway keys new`` and lives in the calling application's secret store.
Clients send it the way they already send an OpenAI key: ``Authorization: Bearer lfg_...``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

KEY_PREFIX = "lfg_"


class KeyRingError(ValueError):
    """Raised when the key file is malformed."""


class ApiKeySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    team: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    projects: list[str] | None = None
    created: date | None = None
    expires: date | None = None
    disabled: bool = False


@dataclass(frozen=True)
class ApiKey:
    id: str
    team: str
    projects: tuple[str, ...] | None
    expires: date | None
    disabled: bool

    def allows_project(self, project: str) -> bool:
        return self.projects is None or project in self.projects

    def is_expired(self, today: date | None = None) -> bool:
        today = today or datetime.now(UTC).date()
        return self.expires is not None and today > self.expires


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str]:
    """Return ``(raw_key, sha256)``. The raw key is never stored by the gateway."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_key(raw)


class KeyRing:
    def __init__(self, keys: list[ApiKeySpec], version: str) -> None:
        self._by_hash: dict[str, ApiKey] = {}
        for spec in keys:
            if spec.sha256 in self._by_hash:
                raise KeyRingError(f"duplicate key hash for id '{spec.id}'")
            self._by_hash[spec.sha256] = ApiKey(
                id=spec.id,
                team=spec.team,
                projects=tuple(spec.projects) if spec.projects is not None else None,
                expires=spec.expires,
                disabled=spec.disabled,
            )
        self.version = version

    @classmethod
    def empty(cls) -> KeyRing:
        return cls([], version="none")

    @classmethod
    def from_yaml(cls, path: Path) -> KeyRing:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise KeyRingError(f"{path}: top level must be a mapping")
        try:
            keys = [ApiKeySpec.model_validate(k) for k in (raw.get("keys") or [])]
        except ValueError as exc:
            raise KeyRingError(f"{path}: {exc}") from exc
        return cls(keys, version=str(raw.get("version", "unversioned")))

    def __len__(self) -> int:
        return len(self._by_hash)

    def lookup(self, raw_key: str) -> ApiKey | None:
        """Constant-time match on the hash; returns the key even if disabled or expired so the
        caller can give a precise error."""
        digest = hash_key(raw_key)
        for stored, key in self._by_hash.items():
            if hmac.compare_digest(stored, digest):
                return key
        return None
