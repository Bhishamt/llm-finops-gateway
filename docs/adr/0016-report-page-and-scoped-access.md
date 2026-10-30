# 0016. A built-in report page, and who may read what

Date: 2026-10-30
Status: Accepted

## Context

The chargeback and budget endpoints returned JSON to anyone who could reach the port. That is
fine on a laptop and wrong anywhere else: a team's spend is that team's business, and the
FinOps practitioner needs all of it. A demo also needs a page, not a curl.

Three audiences were named: developers (my team, my project), team leads (my team's budget)
and FinOps practitioners (every team, trends, exports). The question was how much of that the
open-source core should answer, and where Power BI or another BI tool comes in.

## Decision

- One access rule for every report view, `resolve_report_scope`: in `api_key` mode a team
  key reads its own team only, a key with `role: finops` reads every team (and may filter
  with `X-Team` or `?team=`), no key is 401; in `headers` mode (development) reports are open.
  Keys gain an optional `role` (`team`, the default, or `finops`); `keys new --role finops`.
- `GET /finops` is a server-rendered HTML page: budget state per team with the warn and
  block thresholds, the chargeback table, totals, the price book and policy versions, the
  configured providers. No JavaScript, no build step, refreshes every minute. In `api_key`
  mode the page shows a login form that stores the key in an HttpOnly session cookie.
- `GET /v1/finops/summary.csv` returns the same rows as the JSON summary, for Excel, Power BI
  (Get Data, Web, with an `Authorization` header) and any spreadsheet.
- BI tools are a consumer, not a dependency. The reference pattern is in `docs/reporting.md`:
  the HTML page for day-to-day and demos, CSV or JSON pulled by Power BI for the FinOps team,
  and the ledger database (PostgreSQL on the roadmap) as the direct source once the volume
  or the modelling needs exceed what an endpoint should serve.

## Consequences

- A team key is both the way to call models and the way to see what that team spent;
  nothing new to issue for developers and team leads. FinOps staff get one key with
  `role: finops`.
- Per-user identity on the report (OIDC, SSO groups) is the same future work as per-user
  attribution; the scope rule has one place to grow into it.
- A full showback portal with user management, row-level security in Power BI, approval
  flows and customer branding stays out of the core (enterprise layer).
