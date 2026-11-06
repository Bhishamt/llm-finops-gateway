# How this compares to existing LLM gateways

Last reviewed: 2026-10. Vendor features change quickly; treat this as a snapshot and open a PR
if something is out of date.

## The field

| Tool | Licence / model | Cost and budget features (as documented by the vendor or reviews) |
| --- | --- | --- |
| LiteLLM | Open source proxy, paid enterprise tier | Budgets per key, team and user (`max_budget`, `budget_duration`) with the estimated cost reserved before the call by default; per-request cost from a bundled price table, custom prices per model; Prometheus spend and budget metrics; fallbacks and retries; logging to observability back ends |
| Bifrost (Maxim AI) | Open source core, enterprise tier | Virtual keys with budgets and rate limits by model, provider and time window; logs broken down by key, model, provider, region; audit logs and log export in the enterprise tier |
| Portkey | MIT gateway, closed managed tier | Virtual keys, cost panels per provider and key, budgets |
| Helicone | Apache 2.0, self-hostable | Cost dashboards by provider, model and user; no budget caps documented |
| Langfuse | MIT core, self-hostable | Cost panels next to traces and prompts; observability, not enforcement |
| OpenRouter | Hosted, closed | One invoice across providers, usage by model and key, account-level spend limits |
| Cloudflare AI Gateway | Hosted, closed | Caching, rate limiting per key, model or user, token analytics |
| Azure API Management (GenAI gateway) | Hosted, closed | Token-limit and token-metric policies in front of Azure OpenAI |
| CloudZero, Vantage, Finout | Commercial FinOps platforms | Allocate cloud and LLM spend to teams from billing data; no enforcement at the request path |

## Where the gaps are

Independent write-ups of the gateway category point at the same three things:

- **Chargeback is manual.** Keys are not cost centres. Turning per-key usage into a report
  finance accepts means reconciling logs by hand.
- **Budgets stop at the key, team or user.** Enforcement has become stronger (LiteLLM now
  reserves the estimated cost before the call), but a project and use-case dimension, separate
  warn and block thresholds, and budgets declared in a reviewed file are still rare.
- **Pricing is not versioned and policy is not code.** The price table lives inside the product
  and changes with upgrades; policy lives in an admin UI. Neither is reviewable in a pull
  request or reproducible after the fact.

## What this project does differently

1. **Attribution is a precondition, not a label.** The team comes from a team-bound key
   (hashes in git, raw key in the team's secret store) and `X-Project` is required; a call
   without them is rejected before any provider call (ADR-0003, 0006). There is no
   "unallocated" bucket by construction, and no way to charge another team by editing a
   header.
2. **The price book is data in git.** `config/pricing.yaml` carries a version; every ledger row
   stores the version it was priced with. Recomputing a past month with the prices that applied
   then is a query, not an archaeology exercise (ADR-0002).
3. **Unknown models fail closed.** No price, no call. Enabling a model is a reviewed config
   change, which is also the moment to attach a policy.
4. **Policy as code.** Per-team budgets with separate warn and block thresholds, warn-only
   mode, model allow-lists and requests-per-minute limits in `config/policies.yaml`, keyed on
   the same attribution dimensions, reviewed like any other change. Budgets are enforced by
   an atomic reservation of the estimated cost, so the limit holds under concurrency
   (ADR-0007).
5. **Evaluation replay (ADR-0009, proposed).** Recorded calls can be re-run against a new model or prompt to
   measure quality and cost regression together, which the gateway category leaves to separate
   tools.

## What this project does not try to do

Multi-provider routing, semantic caching, fallbacks, a management UI, prompt management. Run an
existing gateway for those and place this one behind it, or in front of it, depending on where
you want cost enforcement to happen.

## Sources

- [Best AI gateways for cost tracking and chargeback](https://dev.to/tobiaskrell/best-ai-gateways-for-cost-tracking-and-chargeback-dk4)
- [Best LLM cost tracking tools 2026](https://futureagi.com/blog/best-llm-cost-tracking-tools-2026/)
- [Top 5 enterprise gateways for LLM cost tracking and budget controls](https://getmaxim.ai/articles/top-5-enterprise-gateways-for-llm-cost-tracking-and-budget-controls)
- [Best FinOps tools for managing AI costs](https://vantage.sh/blog/best-finops-tools-ai)
- [LiteLLM documentation](https://docs.litellm.ai/); budgets and reservation:
  [team budgets](https://docs.litellm.ai/docs/proxy/team_budgets),
  [users and budgets](https://docs.litellm.ai/docs/proxy/users),
  [custom pricing](https://docs.litellm.ai/docs/proxy/custom_pricing) (checked 2026-11-03)
- [Azure API Management GenAI gateway capabilities](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities)
