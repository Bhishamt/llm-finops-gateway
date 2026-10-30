# ruff: noqa: E501  (HTML and CSS templates)
"""Read-only FinOps reporting (ADR-0016).

Three views of the same ledger, all behind one access rule:

* ``GET /finops``: a server-rendered HTML page with the budget state and the chargeback table;
  the thing to show in a demo or to bookmark.
* ``GET /v1/finops/summary.csv``: the chargeback rows for Excel, Power BI and friends.
* ``GET /v1/finops/summary`` and ``/v1/finops/budgets``: the JSON views that already existed.

Access scope: in ``api_key`` mode a team key sees its own team, a key with ``role: finops`` sees
every team; in ``headers`` mode (development) everything is open. Browsers hold the key in an
HttpOnly session cookie set by the small login form on ``/finops``.
"""

from __future__ import annotations

import csv
import html
import io
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Header, HTTPException, Request

from .auth import ApiKey, KeyRing
from .models import CostSummaryRow, TeamBudgetRow

COOKIE = "lfg_report_key"


@dataclass(frozen=True)
class ReportScope:
    team: str | None  # None: every team
    key_id: str | None = None

    def narrow(self, team: str | None) -> str | None:
        """The team filter to apply: the key's own team wins over any requested filter."""
        return self.team if self.team is not None else team


def _key_from(keyring: KeyRing, raw: str) -> ApiKey:
    key = keyring.lookup(raw)
    if key is None:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    if key.disabled:
        raise HTTPException(status_code=401, detail={"error": "api_key_disabled", "key": key.id})
    if key.is_expired():
        raise HTTPException(status_code=401, detail={"error": "api_key_expired", "key": key.id})
    return key


def scope_for(request: Request, authorization: str | None, x_team: str | None) -> ReportScope:
    settings = request.app.state.settings
    if settings.auth_mode != "api_key":
        return ReportScope(team=x_team or None)
    raw: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    raw = raw or request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "hint": "Reports need a team key (own team) or a key with role finops "
                "(all teams): 'Authorization: Bearer lfg_...'.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    key = _key_from(request.app.state.keyring, raw)
    if key.role == "finops":
        return ReportScope(team=x_team or None, key_id=key.id)
    if x_team and x_team != key.team:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "team_mismatch",
                "message": f"key '{key.id}' belongs to team '{key.team}', not '{x_team}'",
            },
        )
    return ReportScope(team=key.team, key_id=key.id)


def resolve_report_scope(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_team: Annotated[str | None, Header()] = None,
) -> ReportScope:
    """FastAPI dependency for every report endpoint."""
    return scope_for(request, authorization, x_team)


def summary_csv(rows: list[CostSummaryRow], currency: str) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "team",
            "project",
            "model",
            "calls",
            "input_tokens",
            "output_tokens",
            f"cost_{currency.lower()}",
        ]
    )
    for r in rows:
        writer.writerow(
            [r.team, r.project, r.model, r.calls, r.input_tokens, r.output_tokens, r.cost_usd]
        )
    return out.getvalue()


@dataclass(frozen=True)
class ReportContext:
    version: str
    price_book_version: str
    currency: str
    policy_version: str
    providers: str
    month_start: str
    scope: ReportScope
    team_filter: str | None
    budgets: list[TeamBudgetRow]
    rows: list[CostSummaryRow]
    total: str


_STYLE = """
:root { color-scheme: light dark; --ok: #2e7d32; --warn: #ed6c02; --block: #c62828; }
body { font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 24px; max-width: 1200px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; }
.meta { color: #777; margin-bottom: 16px; } .meta span { margin-right: 16px; }
table { border-collapse: collapse; width: 100%; } th, td { padding: 6px 10px; text-align: left; }
th { border-bottom: 2px solid #999; font-weight: 600; } td { border-bottom: 1px solid #ddd; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.level { padding: 1px 8px; border-radius: 10px; color: #fff; font-size: 12px; }
.ok { background: var(--ok); } .warn { background: var(--warn); } .block { background: var(--block); }
.bar { background: #ddd; height: 8px; border-radius: 4px; width: 160px; display: inline-block; vertical-align: middle; margin-right: 8px; }
.bar i { display: block; height: 8px; border-radius: 4px; }
form { margin: 24px 0; } input[type=password] { width: 420px; padding: 6px; }
footer { margin-top: 32px; color: #777; font-size: 12px; }
"""


def _e(value: object) -> str:
    return html.escape(str(value))


def _budget_cells(row: TeamBudgetRow) -> str:
    b = row.budget
    if b is None:
        return "<td colspan=4>no budget</td>"
    pct = min(float(b.used_pct), 100.0)
    color = {"ok": "var(--ok)", "warn": "var(--warn)", "block": "var(--block)"}[b.level]
    bar = f'<span class="bar"><i style="width:{pct:.0f}%;background:{color}"></i></span>'
    return (
        f'<td class="num">{_e(b.spent_usd)}</td><td class="num">{_e(b.limit_usd)}</td>'
        f"<td>{bar}{_e(b.used_pct)} %</td>"
        f'<td><span class="level {b.level}">{b.level}</span> '
        f"<small>warn {_e(b.warn_at_pct)} % / block {_e(b.block_at_pct)} %, {b.enforcement}</small></td>"
    )


def render_report(ctx: ReportContext) -> str:
    scope_text = (f"team <b>{_e(ctx.scope.team)}</b>" if ctx.scope.team else "all teams") + (
        f" (key {_e(ctx.scope.key_id)})" if ctx.scope.key_id else ""
    )
    budget_rows = (
        "".join(
            f"<tr><td>{_e(r.team)}</td>{_budget_cells(r)}"
            f'<td class="num">{_e(r.requests_per_minute) if r.requests_per_minute else "-"}</td>'
            f"<td>{_e(', '.join(r.allowed_models))}</td></tr>"
            for r in ctx.budgets
        )
        or '<tr><td colspan="7">no teams yet</td></tr>'
    )
    cost_rows = (
        "".join(
            f"<tr><td>{_e(r.team)}</td><td>{_e(r.project)}</td><td>{_e(r.model)}</td>"
            f'<td class="num">{r.calls}</td><td class="num">{r.input_tokens}</td>'
            f'<td class="num">{r.output_tokens}</td><td class="num">{_e(r.cost_usd)}</td></tr>'
            for r in ctx.rows
        )
        or '<tr><td colspan="7">no calls recorded this month</td></tr>'
    )
    filter_note = f" for team <b>{_e(ctx.team_filter)}</b>" if ctx.team_filter else ""
    logout = '<a href="/finops/logout">sign out</a>' if ctx.scope.key_id else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LLM FinOps report</title>
<meta http-equiv="refresh" content="60"><style>{_STYLE}</style></head>
<body>
<h1>LLM FinOps gateway: month to date</h1>
<div class="meta"><span>since {_e(ctx.month_start)}</span><span>scope: {scope_text}</span>
<span>price book {_e(ctx.price_book_version)} ({_e(ctx.currency)})</span>
<span>policies {_e(ctx.policy_version)}</span><span>providers {_e(ctx.providers)}</span>
<span>gateway {_e(ctx.version)}</span> {logout}</div>

<h2>Budgets</h2>
<table><thead><tr><th>team</th><th class="num">spent</th><th class="num">limit</th><th>used</th>
<th>status</th><th class="num">req/min</th><th>allowed models</th></tr></thead>
<tbody>{budget_rows}</tbody></table>

<h2>Chargeback{filter_note}</h2>
<table><thead><tr><th>team</th><th>project</th><th>model</th><th class="num">calls</th>
<th class="num">input tokens</th><th class="num">output tokens</th><th class="num">cost {_e(ctx.currency)}</th></tr></thead>
<tbody>{cost_rows}</tbody>
<tfoot><tr><th colspan="6">total</th><th class="num">{_e(ctx.total)}</th></tr></tfoot></table>

<footer>Same data as JSON: <code>/v1/finops/summary</code>, <code>/v1/finops/budgets</code>;
as CSV for Excel or Power BI: <code>/v1/finops/summary.csv</code>. Refreshes every minute.</footer>
</body></html>
"""


def render_login(error: str | None = None) -> str:
    msg = f'<p style="color:var(--block)">{_e(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LLM FinOps report</title>
<style>{_STYLE}</style></head>
<body>
<h1>LLM FinOps gateway: report</h1>
<p>Sign in with a team key (shows that team) or a key with <code>role: finops</code> (shows every team).
The key is kept in a session cookie and never written to the ledger.</p>{msg}
<form method="post" action="/finops/login">
<input type="password" name="key" placeholder="lfg_..." autofocus>
<button type="submit">Open report</button></form>
</body></html>
"""


def login_error(exc: HTTPException) -> str:
    detail: Any = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("error", "unauthorized")).replace("_", " ")
    return str(detail)
