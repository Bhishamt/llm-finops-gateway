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


def budgets(report_url: str, api_key: str | None = None, timeout: float = 10.0) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(f"{report_url.rstrip('/')}/v1/finops/budgets", headers=headers)
    resp.raise_for_status()
    return dict(resp.json())


DOC_GLOBS = ("README.md", "docs/*.md", "docs/adr/*.md")


def docs_context(root: Path, max_chars: int = 400_000) -> str:
    """The repository's documentation as one text block, file by file, for the Q&A panel."""
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
    "the question, say so instead of guessing. Be concise and name the document you relied on.\n"
)
