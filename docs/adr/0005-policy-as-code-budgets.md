# 0005. Policy as code: per-team budgets with warn and block thresholds

Date: 2026-11-08
Status: Accepted. The read-then-act enforcement described below was replaced by the atomic reservation in ADR-0007; thresholds, modes and headers are unchanged.

## Context

Attribution (ADR-0003) tells us who spent what. The next FinOps capability is *budgeting*: a
team should be able to see how far it is into its monthly allowance and, past an agreed point,
be stopped. Existing gateways attach budgets to API keys and manage them in an admin UI; neither
maps onto how finance and platform teams actually review spend.

Design questions:

1. Where does the policy live? A UI, a database, or a file in git?
2. What is the unit of budgeting? Key, user, team, project?
3. What happens at the limit: warn, block, or both, and at which points?
4. Is the check done before or after the upstream call, and on which data?

## Decision

**Policy lives in `config/policies.yaml`**, versioned, reviewed in pull requests like the price
book. A `defaults` block applies to every team; a `teams` block overrides individual keys for
named teams. Unknown keys are rejected at load (`extra="forbid"`) so a typo cannot silently
disable a budget. If the file is absent the gateway runs with no budgets and no allow-lists and
logs a warning at startup.

**The unit is the team** (the `X-Team` tag), because that is the unit finance charges back to.
Project-level budgets are a possible later addition inside the same file format.

**Two thresholds, two behaviours.** `warn_at` (default 0.8) marks the response and the log:
`X-FinOps-Budget-Status: warn`, an `X-FinOps-Warning` header with a human-readable sentence, the
same sentence under `x_finops.policy.warnings`, and a `WARNING` log line. `block_at` (default
1.0) rejects the call with HTTP 403 and a structured body (`error: budget_exceeded`, the team,
model, policy version and full budget status). `enforcement: warn` disables blocking for a team
while keeping the warnings, for experiments and demos. A **model allow-list** per team is
evaluated first and rejects with `error: model_not_allowed`.

**The check runs before the upstream call** on month-to-date spend read from the ledger
(UTC calendar month). The call that crosses a threshold is served; the next one sees the new
state. After the call is recorded, the response carries the *post-call* budget status so a
client always sees where it stands now.

## Consequences

- Budgets and allow-lists are diffable, reviewable and reproducible; a budget bump has an author
  and a reason in git history.
- The month-to-date query is a `SUM` over the ledger per request. Fine for SQLite and small
  deployments; at scale it needs a cached counter. Two concurrent calls near the limit can both
  pass (read-then-act race), so blocking is approximate by a request or two. Both are documented
  limitations; an atomic counter is the planned fix when the ledger moves to PostgreSQL.
- Warn-only mode is a conscious escape hatch. The default is block, in line with ADR-0002 and
  ADR-0003: fail closed unless someone explicitly chose otherwise in a reviewed file.
- Clients that ignore headers still get the warning in the JSON body; clients that ignore both
  will eventually get a 403 with enough information to self-serve.
- `GET /v1/finops/budgets` exposes the same status for every team, which is the starting point
  for a dashboard and for alerting.
