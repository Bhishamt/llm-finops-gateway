"""Helpers for the assistant example: call an OpenAI-compatible endpoint with FinOps
attribution headers, read the ``X-FinOps-*`` receipt back, and load the repository's docs as
the context of the "ask about the gateway" panel. No Streamlit in here, so it is testable."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FINOPS_HEADERS = (
    "X-FinOps-Request-Id",
    "X-FinOps-Cost-USD",
    "X-FinOps-Currency",
    "X-FinOps-Cached-Input-Tokens",
    "X-FinOps-Budget-Limit-USD",
    "X-FinOps-Budget-Spent-USD",
    "X-FinOps-Budget-Used-Pct",
    "X-FinOps-Budget-Status",
    "X-FinOps-Project-Budget-Limit-USD",
    "X-FinOps-Project-Budget-Spent-USD",
    "X-FinOps-Project-Budget-Used-Pct",
    "X-FinOps-Project-Budget-Status",
    "X-FinOps-Warning",
)


@dataclass
class Receipt:
    """What the gateway or the plugin said about one call."""

    status_code: int
    request_id: str | None = None
    cost: str | None = None
    currency: str = "USD"
    cached_input_tokens: int = 0
    team_budget: dict[str, str] = field(default_factory=dict)
    project_budget: dict[str, str] = field(default_factory=dict)
    warning: str | None = None
    error: dict[str, Any] | None = None


def receipt_from_headers(status_code: int, headers: dict[str, str], body: Any = None) -> Receipt:
    h = {k.lower(): v for k, v in headers.items()}
    r = Receipt(status_code=status_code)
    r.request_id = h.get("x-finops-request-id")
    r.cost = h.get("x-finops-cost-usd")
    r.currency = h.get("x-finops-currency", "USD")
    r.cached_input_tokens = int(h.get("x-finops-cached-input-tokens") or 0)
    for scope, prefix in (
        ("team_budget", "x-finops-budget-"),
        ("project_budget", "x-finops-project-budget-"),
    ):
        found = {k[len(prefix) :]: v for k, v in h.items() if k.startswith(prefix)}
        setattr(r, scope, found)
    r.warning = h.get("x-finops-warning")
    if status_code >= 400:
        r.error = _error_detail(body)
    return r


def _error_detail(body: Any) -> dict[str, Any]:
    """The gateway returns ``{"detail": {...}}``; LiteLLM wraps it as
    ``{"error": {"message": ..., "provider_specific_fields": {...}}}``."""
    if not isinstance(body, dict):
        return {"message": str(body)}
    if isinstance(body.get("detail"), dict):
        return dict(body["detail"])
    if isinstance(body.get("detail"), str):
        return {"message": body["detail"]}
    err = body.get("error")
    if isinstance(err, dict):
        fields = err.get("provider_specific_fields")
        return (
            dict(fields) if isinstance(fields, dict) else {"message": err.get("message", str(err))}
        )
    return {"message": json.dumps(body)[:300]}


def chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    team: str | None,
    project: str,
    use_case: str | None,
    max_tokens: int = 300,
    timeout: float = 60.0,
) -> tuple[str | None, Receipt]:
    """One non-streaming chat completion with attribution headers. Returns the assistant text
    (``None`` on rejection) and the receipt read from the response headers."""
    headers = {"Authorization": f"Bearer {api_key}", "X-Project": project}
    if team:
        headers["X-Team"] = team  # the gateway in headers mode; ignored by the plugin
    if use_case:
        headers["X-Use-Case"] = use_case
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
    try:
        body: Any = resp.json()
    except ValueError:
        body = resp.text
    receipt = receipt_from_headers(resp.status_code, dict(resp.headers), body)
    if resp.status_code >= 400:
        return None, receipt
    content = body["choices"][0]["message"].get("content") or ""
    return content, receipt


def budgets(
    report_url: str,
    api_key: str | None = None,
    team: str | None = None,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """``/v1/finops/budgets``: the policy and budget state per team, with the project registry
    of each team. ``team`` narrows a report in headers mode (``X-Team``)."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if team:
        headers["X-Team"] = team
    with httpx.Client(timeout=timeout, transport=transport) as client:
        resp = client.get(f"{report_url.rstrip('/')}/v1/finops/budgets", headers=headers)
    resp.raise_for_status()
    return dict(resp.json())


@dataclass
class Identity:
    """Who a key is, as far as the target can tell: the team it is bound to, its alias and
    the default attribution a LiteLLM key may carry in its metadata."""

    team: str | None
    source: str  # "litellm" (virtual key) or "gateway" (lfg_ key)
    key_alias: str | None = None
    default_project: str | None = None
    default_use_case: str | None = None


def identity(
    target_url: str,
    api_key: str,
    timeout: float = 10.0,
    transport: httpx.BaseTransport | None = None,
) -> Identity | None:
    """Resolve a key against the target. A LiteLLM virtual key answers ``/key/info`` (its own
    record) and ``/team/info``; a gateway team key answers ``/v1/finops/budgets`` with its own
    team only. ``None`` when the target accepts neither."""
    base = target_url.rstrip("/")
    auth = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(timeout=timeout, transport=transport) as client:
        resp = client.get(f"{base}/key/info", headers=auth)
        if resp.status_code == 200 and isinstance(resp.json().get("info"), dict):
            info = resp.json()["info"]
            team = info.get("team_alias")
            if not team and info.get("team_id"):
                t = client.get(
                    f"{base}/team/info", params={"team_id": info["team_id"]}, headers=auth
                )
                if t.status_code == 200:
                    team = (t.json().get("team_info") or {}).get("team_alias")
            defaults = (info.get("metadata") or {}).get("finops") or {}
            return Identity(
                team=team,
                source="litellm",
                key_alias=info.get("key_alias"),
                default_project=defaults.get("project"),
                default_use_case=defaults.get("use_case"),
            )
        resp = client.get(f"{base}/v1/finops/budgets", headers=auth)
        if resp.status_code == 200:
            teams = resp.json().get("teams") or []
            return Identity(team=teams[0]["team"] if len(teams) == 1 else None, source="gateway")
    return None


DOC_GLOBS = ("README.md", "docs/architecture.md", "docs/litellm.md", "docs/reporting.md")


def docs_context(root: Path, max_chars: int = 80_000) -> str:
    """The repository's documentation as one text block, file by file, as the system prompt of
    the assistant: the README and the three pages that explain the gateway, about 15k tokens,
    so that a question costs a fraction of a cent and the prefix is served from the provider's
    prompt cache from the second question on."""
    parts: list[str] = []
    total = 0
    for pattern in DOC_GLOBS:
        for path in sorted(root.glob(pattern)):
            text = path.read_text(encoding="utf-8", errors="replace")
            chunk = f"\n\n===== {path.relative_to(root)} =====\n{text}"
            if total + len(chunk) > max_chars:
                break
            parts.append(chunk)
            total += len(chunk)
    return "".join(parts)


QA_SYSTEM = (
    "You answer questions about the llm-finops-gateway project, an open-source LLM FinOps "
    "gateway and LiteLLM plugin. Answer from the documentation below; when it does not cover "
    "the question, say so instead of guessing. Be concise and name the document you relied on. "
    "A question unrelated to the project gets a one-sentence answer and a pointer back to the "
    "documentation.\n"
)
