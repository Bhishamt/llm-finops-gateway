"""Cost attribution.

Design decision (ADR-0003): every call must carry an owner. Unattributed spend is rejected with
HTTP 400, never guessed or dumped into a shared bucket.

Design decision (ADR-0006): where the owner comes from depends on ``auth_mode``:

* ``api_key`` (default): the team is derived from a team-bound API key sent as
  ``Authorization: Bearer lfg_...``. An ``X-Team`` header, if present, must match the key.
* ``headers``: the team is taken from ``X-Team`` as declared. For development and networks
  where the caller is already authenticated upstream.

``X-Project`` is always required; ``X-Use-Case`` is optional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated

from fastapi import Header, HTTPException, Request

from .auth import KeyRing

_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class Attribution:
    team: str
    project: str
    use_case: str = "unspecified"
    key_id: str | None = None


def _validate_tag(name: str, value: str) -> str:
    if not _TAG_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must match {_TAG_RE.pattern} (got {value!r})",
        )
    return value


def _require_project(x_project: str | None) -> str:
    if not x_project:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_attribution",
                "missing_headers": ["X-Project"],
                "hint": "Set X-Project so the call can be charged back.",
            },
        )
    return _validate_tag("X-Project", x_project)


def attribution_from_headers(
    x_team: str | None,
    x_project: str | None,
    x_use_case: str | None,
) -> Attribution:
    """``headers`` mode: the caller declares the team."""
    missing = [name for name, value in (("X-Team", x_team), ("X-Project", x_project)) if not value]
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_attribution",
                "missing_headers": missing,
                "hint": "Set X-Team and X-Project so the call can be charged back.",
            },
        )
    assert x_team is not None and x_project is not None
    return Attribution(
        team=_validate_tag("X-Team", x_team),
        project=_validate_tag("X-Project", x_project),
        use_case=_validate_tag("X-Use-Case", x_use_case) if x_use_case else "unspecified",
    )


def attribution_from_api_key(
    keyring: KeyRing,
    authorization: str | None,
    x_team: str | None,
    x_project: str | None,
    x_use_case: str | None,
) -> Attribution:
    """``api_key`` mode: the team comes from the key; headers may only narrow, never override."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "hint": "Send your team key as 'Authorization: Bearer lfg_...' "
                "(the OpenAI SDK's api_key parameter).",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    key = keyring.lookup(authorization[7:].strip())
    if key is None:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    if key.disabled:
        raise HTTPException(status_code=401, detail={"error": "api_key_disabled", "key": key.id})
    if key.is_expired():
        raise HTTPException(status_code=401, detail={"error": "api_key_expired", "key": key.id})
    if x_team and x_team != key.team:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "team_mismatch",
                "message": f"key '{key.id}' belongs to team '{key.team}', not '{x_team}'",
            },
        )
    project = _require_project(x_project)
    if not key.allows_project(project):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "project_not_allowed",
                "message": (
                    f"key '{key.id}' may only be used for projects {list(key.projects or [])}"
                ),
            },
        )
    return Attribution(
        team=key.team,
        project=project,
        use_case=_validate_tag("X-Use-Case", x_use_case) if x_use_case else "unspecified",
        key_id=key.id,
    )


def resolve_attribution(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_team: Annotated[str | None, Header()] = None,
    x_project: Annotated[str | None, Header()] = None,
    x_use_case: Annotated[str | None, Header()] = None,
) -> Attribution:
    """FastAPI dependency; picks the mode from app state."""
    if request.app.state.settings.auth_mode == "api_key":
        keyring: KeyRing = request.app.state.keyring
        return attribution_from_api_key(keyring, authorization, x_team, x_project, x_use_case)
    return attribution_from_headers(x_team, x_project, x_use_case)
