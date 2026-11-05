# 0019. The FinOps rules as a LiteLLM plugin

Date: 2026-11-05
Status: Accepted

## Context

Many organisations already run an LLM gateway, most often LiteLLM, and will not put a second
proxy in front of it. LiteLLM routes, balances, caches and keeps keys, teams and budgets, but
its FinOps record has the gaps this project was built to close: a model missing from its price
map is forwarded and logged at zero cost (and a zero-cost model skips the budget checks), the
owner of a call is the key or team and nothing finer, prices live in the product rather than
in a reviewed file, and nothing reconciles the log against the bill. The LiteLLM proxy exposes
hooks that run before and after every call, so the rules of ADR-0002, 0003, 0005 and 0007 can
run inside it without a second network hop.

## Decision

- The gateway's rules ship as a LiteLLM proxy plugin in this repository:
  `llm_finops_gateway.litellm_plugin`, installed with the optional extra `[litellm]`, which
  pins LiteLLM to a tested minor version. A contract test asserts the hook signatures of the
  pinned version so that a bump fails in CI, not at a customer.
- The plugin is a `CustomLogger` instance registered under `litellm_settings.callbacks`. It
  uses three hooks: `async_pre_call_hook` to reject or reserve before the upstream call,
  `async_post_call_response_headers_hook` to add the `X-FinOps-*` headers, and the
  success and failure log events to settle or release. Streaming settles from the final usage
  that LiteLLM reports when the stream completes.
- Attribution: the team is the LiteLLM team alias of the calling key (LiteLLM has already
  authenticated it), the project and use case come from `X-Project` and `X-Use-Case` exactly
  as in `headers` mode. A call without a project is rejected with 400 `missing_attribution`.
- Pricing: the plugin prices from this project's price book. A model the book does not know is
  rejected with 422 before the call (ADR-0002). LiteLLM's own `response_cost` is recorded next
  to the ledger cost for comparison, never used for billing.
- Budgets: `config/policies.yaml` is the source of truth for warn and block, enforced with the
  same atomic reservation in `budget_counters` (ADR-0007). LiteLLM budgets may stay configured
  as a second, coarser guard; a deployment should pick one of the two as the number teams are
  told about.
- Ledger: the same tables and the same `Ledger` class, so the report page, the CSV, the
  summary API and `ledger reconcile` see LiteLLM traffic without change. The row's provider is
  the provider LiteLLM resolved for the call.
- The response body is not modified: there is no `x_finops` block in LiteLLM mode, the
  headers carry the same fields.
- The request path in `main.py` is refactored first so that estimate, reserve, settle and
  release are plain functions shared by the FastAPI gateway and the plugin; the gateway's
  behaviour (status codes, headers, `x_finops`) does not change.
- Out of scope for this ADR: the LiteLLM dashboard UI plugin for the report page, identity
  from OIDC claims, and multi-currency price books; each is its own decision.

## Consequences

- A LiteLLM user gets fail-closed pricing, mandatory project attribution, reviewed budgets and
  bill reconciliation by adding one callback and two config files, with no new proxy.
- Two code paths share one rule set; the refactor is the price of that, and the existing test
  suite guards it.
- The plugin follows LiteLLM's hook contract, which is not a stable public API; the pin and the
  contract test make a breaking change visible before release.
- Latency of the hooks is measured, not assumed, before any number is quoted.
