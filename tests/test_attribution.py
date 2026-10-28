from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from llm_finops_gateway.attribution import Attribution, attribution_from_headers


def test_full_headers_build_attribution() -> None:
    attr = attribution_from_headers("platform", "demo", "chat")
    assert attr == Attribution(team="platform", project="demo", use_case="chat")


def test_use_case_defaults_to_unspecified() -> None:
    assert attribution_from_headers("platform", "demo", None).use_case == "unspecified"


@pytest.mark.parametrize(
    ("team", "project", "missing"),
    [
        (None, "demo", ["X-Team"]),
        ("platform", None, ["X-Project"]),
        (None, None, ["X-Team", "X-Project"]),
        ("", "demo", ["X-Team"]),
    ],
)
def test_missing_headers_are_rejected(
    team: str | None, project: str | None, missing: list[str]
) -> None:
    with pytest.raises(HTTPException) as exc:
        attribution_from_headers(team, project, None)
    assert exc.value.status_code == 400
    detail: Any = exc.value.detail  # FastAPI allows dict details; Starlette types it as str
    assert detail["missing_headers"] == missing


@pytest.mark.parametrize("bad", ["has space", "semi;colon", "-leading", "x" * 65, "ünïcode"])
def test_invalid_tag_values_are_rejected(bad: str) -> None:
    with pytest.raises(HTTPException) as exc:
        attribution_from_headers(bad, "demo", None)
    assert exc.value.status_code == 400


def test_attribution_is_immutable() -> None:
    attr = Attribution(team="a", project="b")
    with pytest.raises(AttributeError):
        attr.team = "c"  # type: ignore[misc]
