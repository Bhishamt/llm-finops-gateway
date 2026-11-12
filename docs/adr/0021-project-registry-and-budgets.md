# 0021. Projects and use cases are registered in the policy file, with their own budgets

Date: 2026-11-05
Status: Accepted

## Context

Attribution is mandatory (ADR-0003), but the project and use-case values are free text: any
string that matches the tag pattern is accepted. That is enough to make spend attributable,
and not enough to make it governable. A team gets one key, a new project starts inside the
team with its own budget line, and nobody can say from the ledger alone whether
`chat-assistant` and `chat_assistant` are the same thing or whether `pilot-7` was ever
approved. Budgets today stop at the team (ADR-0005), so a project cannot be given a limit of
its own.

## Decision

- The policy file can register a team's projects: a `projects` map under the team. When the
  map is present, a call with a project that is not in it is rejected before the upstream
  call with `403 project_not_registered`; when it is absent, every well-formed project is
  accepted as before. Registering a project is therefore a reviewed change to
  `policies.yaml`, the same workflow as a budget change.
- A registered project may carry its own `budget` (same shape as a team budget: monthly
  amount, warn and block thresholds, enforcement, reserve size), its own `allowed_models`
  (which narrows the team's list, never widens it) and a `use_cases` list. When `use_cases`
  is present, a use case outside it is rejected with `403 use_case_not_registered`.
- Project budgets are enforced like team budgets: the estimated cost is reserved
  atomically before the call, settled with the ledger row and released on failure
  (ADR-0007). The team's reservation is taken first; if the project's reservation fails, the
  team's is released and the call is rejected with `budget_exceeded` naming the project.
  Warn levels of both scopes are reported.
- Budget counters are keyed by scope: the team name for the team's counter and
  `team/project` for a project's counter, in the existing `budget_counters` table. No schema
  migration; the `team` column of that table is a scope key from now on.
- The response carries the project's budget state next to the team's:
  `x_finops.policy.project_budget` and `X-FinOps-Project-Budget-*` headers; the report page
  and summary API already group by project.
- The same rules run in the LiteLLM plugin (ADR-0019) through the shared policy engine.

## Consequences

- A new project is a pull request before it is a line in the ledger: the registry is the
  approval record, and finance can read it.
- Teams that do not want the registry change nothing; the free-text behaviour is the
  default.
- Two reservations per call where a project budget exists; the cost is one extra row update
  in the same database.
- Rate limits stay per team; per-project rate limits are not in this decision.
