# ruff: noqa: E501  (HTML, CSS and JavaScript templates)
"""Read-only FinOps reporting (ADR-0016).

Three views of the same ledger, all behind one access rule:

* ``GET /finops``: a server-rendered HTML page with the budget state and the chargeback table;
  the thing to show in a demo or to bookmark. A few hundred lines of inline JavaScript add
  sorting, filtering, a team selector, a display currency and the policy / price book views;
  the default view renders without it.
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
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Header, HTTPException, Request

from .auth import ApiKey, KeyRing
from .litellm_ui import SESSION_COOKIE, verify_session
from .models import CostSummaryRow, TeamBudgetRow
from .pricing import money

COOKIE = "lfg_report_key"
PLUGIN_COOKIE = SESSION_COOKIE


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
    if settings.litellm_plugin_name and settings.litellm_salt_key:
        # A LiteLLM dashboard session (ADR-0024) sees every team, like a finops key.
        session = verify_session(
            settings.litellm_salt_key.get_secret_value(),
            settings.litellm_plugin_name,
            request.cookies.get(PLUGIN_COOKIE),
        )
        if session is not None:
            return ReportScope(team=x_team or None, key_id=f"litellm:{session['user_id']}")
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


# ---- page data -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyRow:
    team: str  # "(defaults)" for the fallback policy
    allowed_models: list[str]
    monthly: Decimal | None
    warn_pct: str | None
    block_pct: str | None
    enforcement: str | None
    reserve_output_tokens: int | None
    requests_per_minute: int | None


@dataclass(frozen=True)
class PriceRow:
    model: str
    provider: str | None
    input: Decimal
    output: Decimal
    cached_input: Decimal | None
    reasoning: bool


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
    teams: list[str] = field(default_factory=list)
    rates: dict[str, float] = field(
        default_factory=dict
    )  # display currencies: code -> per 1 book unit
    policy_rows: list[PolicyRow] = field(default_factory=list)
    policy_yaml: str | None = None
    price_rows: list[PriceRow] = field(default_factory=list)


# ---- rendering -----------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --ok: #2e7d32; --warn: #ed6c02; --block: #c62828; --muted: #777; --line: #ddd; }
body { font: 14px/1.45 system-ui, sans-serif; margin: 0; padding: 24px; max-width: 1280px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; display: flex; align-items: center; gap: 12px; }
.meta { color: var(--muted); margin-bottom: 12px; } .meta span { margin-right: 16px; }
.meta a { color: inherit; }
.toolbar { display: flex; flex-wrap: wrap; gap: 16px 24px; align-items: center; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; margin: 12px 0 4px; }
.toolbar label { margin-right: 6px; white-space: nowrap; }
.dropdown { position: relative; display: inline-block; }
.dropdown button { font: inherit; padding: 4px 10px; cursor: pointer; }
.dropdown .menu { position: absolute; z-index: 10; top: 110%; left: 0; min-width: 240px; max-height: 320px; overflow: auto; background: Canvas; color: CanvasText; border: 1px solid #999; border-radius: 6px; padding: 8px 10px; box-shadow: 0 4px 16px rgba(0,0,0,.2); }
.dropdown .menu label { display: block; margin: 2px 0; white-space: nowrap; } .dropdown .menu hr { border: 0; border-top: 1px solid var(--line); margin: 6px 0; }
table { border-collapse: collapse; width: 100%; } th, td { padding: 6px 10px; text-align: left; vertical-align: top; }
th { border-bottom: 2px solid #999; font-weight: 600; } td { border-bottom: 1px solid var(--line); }
th[data-key] { cursor: pointer; user-select: none; } th[data-key]:after { content: " \\2195"; color: var(--muted); font-size: 11px; }
th.asc:after { content: " \\2191"; } th.desc:after { content: " \\2193"; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.level { padding: 1px 8px; border-radius: 10px; color: #fff; font-size: 12px; }
.ok { background: var(--ok); } .warn { background: var(--warn); } .block { background: var(--block); }
.bar { background: var(--line); height: 8px; border-radius: 4px; width: 160px; display: inline-block; vertical-align: middle; margin-right: 8px; }
.bar i { display: block; height: 8px; border-radius: 4px; }
input.filter { font: inherit; padding: 3px 8px; width: 220px; }
details { margin: 12px 0; } summary { cursor: pointer; font-weight: 600; }
pre { background: rgba(127,127,127,.12); padding: 12px; border-radius: 6px; overflow: auto; font-size: 12px; }
form { margin: 24px 0; } input[type=password] { width: 420px; padding: 6px; }
footer { margin-top: 32px; color: var(--muted); font-size: 12px; }
small, .muted { color: var(--muted); }
"""

_SCRIPT = r"""
(function () {
  var S = {};
  try { S = JSON.parse(localStorage.getItem('lfg-report') || '{}'); } catch (e) { S = {}; }
  function save() { try { localStorage.setItem('lfg-report', JSON.stringify(S)); } catch (e) {} }

  // ---- display currency: every .amt cell carries the book amount in data-amt ----
  var rates = JSON.parse(document.getElementById('rates').textContent);
  var sel = document.getElementById('currency');
  function fmt(v) { var s = v.toFixed(7).replace(/0+$/, '').replace(/\.$/, ''); return s === '-0' ? '0' : s; }
  function rate() { return rates[sel.value] || 1; }
  function applyCurrency() {
    document.querySelectorAll('.amt').forEach(function (el) {
      var base = parseFloat(el.getAttribute('data-amt'));
      if (!isNaN(base)) el.textContent = fmt(base * rate());
    });
    document.querySelectorAll('.cur').forEach(function (el) { el.textContent = sel.value; });
    S.currency = sel.value; save();
  }
  if (S.currency && rates[S.currency] !== undefined) sel.value = S.currency;
  sel.addEventListener('change', applyCurrency);

  // ---- team selector: a dropdown with "all teams" and one checkbox per team ----
  var boxes = Array.prototype.slice.call(document.querySelectorAll('input.team'));
  var all = document.getElementById('team-all');
  var btn = document.getElementById('team-btn'), menu = document.getElementById('team-menu');
  function selected() { return boxes.filter(function (b) { return b.checked; }).map(function (b) { return b.value; }); }
  function label(on) {
    if (!boxes.length || on.length === boxes.length) return 'team: all teams';
    if (on.length === 0) return 'team: none';
    return 'team: ' + (on.length <= 2 ? on.join(', ') : on.length + ' of ' + boxes.length);
  }
  function applyTeams() {
    var on = selected();
    document.querySelectorAll('tr[data-team]').forEach(function (tr) {
      tr.classList.toggle('team-hidden', on.indexOf(tr.getAttribute('data-team')) < 0);
    });
    all.checked = boxes.length === 0 || on.length === boxes.length;
    btn.textContent = label(on) + ' \u25BE';
    S.teams = on; save(); applyFilters();
  }
  if (Array.isArray(S.teams) && boxes.length) boxes.forEach(function (b) { b.checked = S.teams.indexOf(b.value) >= 0; });
  all.addEventListener('change', function () { boxes.forEach(function (b) { b.checked = all.checked; }); applyTeams(); });
  boxes.forEach(function (b) { b.addEventListener('change', applyTeams); });
  btn.addEventListener('click', function (ev) { ev.stopPropagation(); menu.hidden = !menu.hidden; });
  menu.addEventListener('click', function (ev) { ev.stopPropagation(); });
  document.addEventListener('click', function () { menu.hidden = true; });

  // ---- text filter per table, and the total of the visible chargeback rows ----
  function applyFilters() {
    document.querySelectorAll('input.filter').forEach(function (inp) {
      var table = document.getElementById(inp.getAttribute('data-table'));
      var q = inp.value.trim().toLowerCase();
      table.querySelectorAll('tbody tr').forEach(function (tr) {
        var hit = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
        tr.style.display = (hit && !tr.classList.contains('team-hidden')) ? '' : 'none';
      });
    });
    var total = 0, n = 0;
    document.querySelectorAll('#chargeback tbody tr').forEach(function (tr) {
      if (tr.style.display === 'none') return;
      var c = tr.querySelector('td.cost'); if (c) { total += parseFloat(c.getAttribute('data-amt')) || 0; n++; }
    });
    var t = document.getElementById('total'); if (t) { t.setAttribute('data-amt', String(total)); }
    var cnt = document.getElementById('row-count'); if (cnt) { cnt.textContent = n + ' row' + (n === 1 ? '' : 's'); }
    applyCurrency();
  }
  document.querySelectorAll('input.filter').forEach(function (inp) { inp.addEventListener('input', applyFilters); });

  // ---- sorting: click a header; numeric cells sort by data-amt / data-v or their number.
  //      The choice is kept per table, so a reload only refreshes the data. ----
  function sortTable(table, idx, asc) {
    var tbody = table.querySelector('tbody'), ths = table.querySelectorAll('thead th');
    ths.forEach(function (h) { h.classList.remove('asc', 'desc'); });
    if (ths[idx]) ths[idx].classList.add(asc ? 'asc' : 'desc');
    var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
    rows.sort(function (a, b) {
      var x = a.children[idx], y = b.children[idx];
      if (!x || !y) return 0;
      var xv = x.getAttribute('data-amt') || x.getAttribute('data-v') || x.textContent.trim();
      var yv = y.getAttribute('data-amt') || y.getAttribute('data-v') || y.textContent.trim();
      var xn = parseFloat(xv), yn = parseFloat(yv);
      var r = (!isNaN(xn) && !isNaN(yn)) ? xn - yn : xv.localeCompare(yv);
      return asc ? r : -r;
    });
    rows.forEach(function (r) { tbody.appendChild(r); });
  }
  S.sort = S.sort || {};
  document.querySelectorAll('table.sortable th[data-key]').forEach(function (th) {
    th.addEventListener('click', function () {
      var table = th.closest('table');
      var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
      var asc = !th.classList.contains('asc');
      sortTable(table, idx, asc);
      S.sort[table.id] = { idx: idx, asc: asc }; save();
    });
  });
  Object.keys(S.sort).forEach(function (id) {
    var table = document.getElementById(id), st = S.sort[id];
    if (table && st) sortTable(table, st.idx, st.asc);
  });

  // ---- policy and price book: the version links open the details sections ----
  document.querySelectorAll('a.open').forEach(function (a) {
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      var d = document.getElementById(a.getAttribute('data-target')); d.open = true; d.scrollIntoView({ behavior: 'smooth' });
    });
  });
  applyTeams();
})();
"""


def _e(value: object) -> str:
    return html.escape(str(value))


def _amt(value: Decimal | str | None, extra_class: str = "") -> str:
    if value is None:
        return '<td class="num">-</td>'
    return (
        f'<td class="num amt {extra_class}" data-amt="{_e(money(value))}">{_e(money(value))}</td>'
    )


def _enforcement_note(warn_pct: str, block_pct: str, enforcement: str) -> str:
    """What happens at the thresholds, in words: a warn-only team is never blocked."""
    if enforcement == "warn":
        return f"warn from {_e(warn_pct)} %, warn only above {_e(block_pct)} %, never blocked"
    return f"warn from {_e(warn_pct)} %, blocked at {_e(block_pct)} %"


def _budget_cells(row: TeamBudgetRow) -> str:
    b = row.budget
    if b is None:
        return "<td colspan=4>no budget</td>"
    pct = min(float(b.used_pct), 100.0)
    color = {"ok": "var(--ok)", "warn": "var(--warn)", "block": "var(--block)"}[b.level]
    bar = f'<span class="bar"><i style="width:{pct:.0f}%;background:{color}"></i></span>'
    return (
        f"{_amt(b.spent_usd)}{_amt(b.limit_usd)}"
        f'<td data-v="{_e(b.used_pct)}">{bar}{_e(b.used_pct)} %</td>'
        f'<td data-v="{b.level}"><span class="level {b.level}">{b.level}</span> '
        f"<small>{_enforcement_note(b.warn_at_pct, b.block_at_pct, b.enforcement)}</small></td>"
    )


def _policy_table(rows: list[PolicyRow]) -> str:
    body = "".join(
        f"<tr><td>{_e(r.team)}</td><td>{_e(', '.join(r.allowed_models))}</td>"
        f"{_amt(r.monthly)}"
        f'<td class="num">{_e(r.warn_pct) if r.warn_pct else "-"}</td>'
        f'<td class="num">{_e(r.block_pct) if r.block_pct else "-"}</td>'
        f"<td>{'warn only (never blocked)' if r.enforcement == 'warn' else _e(r.enforcement) if r.enforcement else '-'}</td>"
        f'<td class="num">{r.reserve_output_tokens if r.reserve_output_tokens else "-"}</td>'
        f'<td class="num">{r.requests_per_minute if r.requests_per_minute else "-"}</td></tr>'
        for r in rows
    )
    return (
        '<table class="sortable" id="policy-table"><thead><tr><th data-key>team</th><th data-key>allowed models</th>'
        '<th class="num" data-key>monthly budget <span class="cur"></span></th><th class="num" data-key>warn %</th>'
        '<th class="num" data-key>block %</th><th data-key>enforcement</th><th class="num" data-key>reserve tokens</th>'
        '<th class="num" data-key>req/min</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def _price_table(rows: list[PriceRow]) -> str:
    body = "".join(
        f"<tr><td>{_e(r.model)}</td><td>{_e(r.provider or 'default')}</td>"
        f"{_amt(r.input)}{_amt(r.output)}{_amt(r.cached_input)}"
        f"<td>{'yes' if r.reasoning else ''}</td></tr>"
        for r in rows
    )
    return (
        '<table class="sortable" id="price-table"><thead><tr><th data-key>model</th><th data-key>provider</th>'
        '<th class="num" data-key>input / 1M <span class="cur"></span></th><th class="num" data-key>output / 1M <span class="cur"></span></th>'
        '<th class="num" data-key>cached input / 1M <span class="cur"></span></th><th data-key>reasoning</th></tr></thead>'
        f"<tbody>{body}</tbody></table>"
    )


def render_report(ctx: ReportContext) -> str:
    scope_text = (f"team <b>{_e(ctx.scope.team)}</b>" if ctx.scope.team else "all teams") + (
        f" (key {_e(ctx.scope.key_id)})" if ctx.scope.key_id else ""
    )
    budget_rows = (
        "".join(
            f'<tr data-team="{_e(r.team)}"><td>{_e(r.team)}</td>{_budget_cells(r)}'
            f'<td class="num">{_e(r.requests_per_minute) if r.requests_per_minute else "-"}</td>'
            f"<td>{_e(', '.join(r.allowed_models))}</td></tr>"
            for r in ctx.budgets
        )
        or '<tr><td colspan="7">no teams yet</td></tr>'
    )
    cost_rows = (
        "".join(
            f'<tr data-team="{_e(r.team)}"><td>{_e(r.team)}</td><td>{_e(r.project)}</td><td>{_e(r.model)}</td>'
            f'<td class="num">{r.calls}</td><td class="num">{r.input_tokens}</td>'
            f'<td class="num">{r.output_tokens}</td>{_amt(r.cost_usd, "cost")}</tr>'
            for r in ctx.rows
        )
        or '<tr><td colspan="7">no calls recorded this month</td></tr>'
    )
    filter_note = f" for team <b>{_e(ctx.team_filter)}</b>" if ctx.team_filter else ""
    logout = '<a href="/finops/logout">sign out</a>' if ctx.scope.key_id else ""
    currencies = "".join(
        f'<option value="{_e(code)}">{_e(code)}</option>'
        for code in [ctx.currency, *sorted(c for c in ctx.rates if c != ctx.currency)]
    )
    rates = {ctx.currency: 1.0, **{k: float(v) for k, v in ctx.rates.items()}}
    rate_note = (
        f"<small>display only, fixed rates from LFG_REPORT_RATES; the ledger is in {_e(ctx.currency)}</small>"
        if ctx.rates
        else "<small>set LFG_REPORT_RATES to add display currencies</small>"
    )
    team_boxes = "".join(
        f'<label><input type="checkbox" class="team" value="{_e(t)}" checked> {_e(t)}</label>'
        for t in ctx.teams
    )
    policy_yaml = (
        f"<details><summary>policies.yaml as loaded</summary><pre>{_e(ctx.policy_yaml)}</pre></details>"
        if ctx.policy_yaml
        else '<p class="muted">no policy file loaded: every team gets no budget and every priced model.</p>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>LLM FinOps report</title>
<style>{_STYLE}</style></head>
<body>
<h1>LLM FinOps gateway: month to date</h1>
<div class="meta"><span>since {_e(ctx.month_start)}</span><span>scope: {scope_text}</span>
<span>price book <a href="#pricebook" class="open" data-target="pricebook">{_e(ctx.price_book_version)}</a> ({_e(ctx.currency)})</span>
<span>policies <a href="#policy" class="open" data-target="policy">{_e(ctx.policy_version)}</a></span>
<span>providers {_e(ctx.providers)}</span><span>gateway {_e(ctx.version)}</span>
<span><a href="/finops">reload data</a></span> {logout}</div>

<div class="toolbar">
  <div><label for="currency">currency</label><select id="currency">{currencies}</select> {rate_note}</div>
  <div class="dropdown"><button type="button" id="team-btn">team: all teams &#9662;</button>
    <div class="menu" id="team-menu" hidden><label><input type="checkbox" id="team-all" checked> <b>all teams</b></label><hr>{team_boxes}</div></div>
</div>
<script type="application/json" id="rates">{json.dumps(rates)}</script>

<h2>Budgets <input class="filter" data-table="budgets" placeholder="filter rows"></h2>
<table class="sortable" id="budgets"><thead><tr><th data-key>team</th><th class="num" data-key>spent <span class="cur"></span></th><th class="num" data-key>limit <span class="cur"></span></th><th data-key>used</th>
<th data-key>status</th><th class="num" data-key>req/min</th><th data-key>allowed models</th></tr></thead>
<tbody>{budget_rows}</tbody></table>

<h2>Chargeback{filter_note} <input class="filter" data-table="chargeback" placeholder="filter rows"> <small id="row-count"></small></h2>
<table class="sortable" id="chargeback"><thead><tr><th data-key>team</th><th data-key>project</th><th data-key>model</th><th class="num" data-key>calls</th>
<th class="num" data-key>input tokens</th><th class="num" data-key>output tokens</th><th class="num" data-key>cost <span class="cur"></span></th></tr></thead>
<tbody>{cost_rows}</tbody>
<tfoot><tr><th colspan="6">total (visible rows)</th><th class="num amt" id="total" data-amt="{_e(ctx.total)}">{_e(ctx.total)}</th></tr></tfoot></table>

<details id="policy"><summary>Policies {_e(ctx.policy_version)}: what applies to each team</summary>
<p class="muted">Teams not listed in the file get the defaults row. Budgets are month to date (UTC); warn and block are fractions of the monthly amount.</p>
{_policy_table(ctx.policy_rows)}
{policy_yaml}
</details>

<details id="pricebook"><summary>Price book {_e(ctx.price_book_version)}: prices per 1M tokens</summary>
<p class="muted">A model not listed here is rejected before any upstream call. Cached input is billed at its own price when the provider reports cached tokens.</p>
{_price_table(ctx.price_rows)}
</details>

<footer>Same data as JSON: <code>/v1/finops/summary</code>, <code>/v1/finops/budgets</code>;
as CSV for Excel or Power BI: <code>/v1/finops/summary.csv</code>. Click a column header to sort; sort order, team selection, filters and currency are kept in this browser across reloads.</footer>
<script>{_SCRIPT}</script>
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
