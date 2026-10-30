# Reporting: the built-in page, spreadsheets, Power BI

The ledger has one row per call. Everything below is a view of it (ADR-0016).

## Who sees what

| Caller | `api_key` mode (production) | `headers` mode (development) |
| --- | --- | --- |
| Team key (`role: team`, default) | its own team, every project | everything |
| Key with `role: finops` | every team; filter with `X-Team` or `?team=` | everything |
| No key | 401 (JSON) or the login form (HTML page) | everything |

Create a FinOps key the same way as a team key:

```bash
llm-finops-gateway keys new --team finops --id finops-reporting --role finops
```

The key's own team name does not matter for reading; `role: finops` is what grants the
all-teams view. Rotate and disable it like any other key.

## The page: `GET /finops`

Open `http://<gateway>/finops` in a browser. In `api_key` mode it asks for a key once and keeps
it in an HttpOnly session cookie; `/finops/logout` clears it. The page shows:

- budgets per team: spent, limit, used %, warn and block thresholds, enforcement, requests per
  minute, allowed models;
- the chargeback table (team, project, model, calls, tokens, cost) and the total;
- price book version and currency, policy version, configured providers, gateway version.

The default view is server-rendered; a few hundred lines of inline JavaScript (no build step,
no external files) add: sorting on any column, a text filter per table, a team selector (all
teams or any subset, the total follows), a display currency switch (`LFG_REPORT_RATES`, for
example `{"EUR": 0.88}`; display only, the ledger stays in the book's currency), and the
policy and price book views behind their version links: the effective policy per team with
the defaults row, the raw `policies.yaml`, and prices per 1M tokens. Selections are kept in
the browser's local storage.

## Spreadsheets and Power BI

`GET /v1/finops/summary.csv` returns the chargeback rows as CSV with the same access rule.

- **Excel**: Data, From Web, the URL; in the advanced dialog add the header
  `Authorization: Bearer lfg_...`.
- **Power BI Desktop**: Get Data, Web, Advanced; URL `https://<gateway>/v1/finops/summary.csv`
  (or the JSON endpoint) and the HTTP header `Authorization` = `Bearer lfg_...`. Use a key
  with `role: finops` so the model holds every team; build row-level security in Power BI if
  team leads should only see their rows there. Schedule the refresh in the Power BI service
  through a gateway or a public endpoint.
- **Power BI with history and trends**: point Power BI at the ledger database instead of the
  endpoint. The `cost_records` table has the timestamp, team, project, use case, model,
  provider, token counts, cost and price book version per call. SQLite is fine for a laptop;
  for a shared deployment use PostgreSQL (`LFG_DATABASE_URL`) and a read-only database user.

## What belongs where

- Core (this repository): the access rule, the HTML page, CSV and JSON, the ledger schema.
- Later in core: per-user identity on the report through OIDC groups, a FOCUS-format export,
  a Grafana dashboard for the Prometheus metrics (operational view, not chargeback).
- Enterprise layer: the Power BI model with row-level security and SSO, customer branding,
  approval workflows for budget changes, ERP exports.
