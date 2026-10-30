# 0011. Several providers in one gateway, chosen per model by the price book

Date: 2026-10-30
Status: Accepted

## Context

Until now one gateway process served one upstream (`LFG_PROVIDER`). A platform team that runs
Azure OpenAI for the large models and Ollama or vLLM on-prem for the small ones had to run two
gateways and point clients at the right one, which moves the "which model lives where" decision
back into every application. ADR-0001 kept routing out of scope; this record narrows that: a
static map from model to upstream is configuration, not routing.

The price book already owns the client-facing model name and the rule that an unknown model is
rejected before any upstream call (ADR-0002). It is the one place every model is listed and
reviewed, so it is the natural place to say which adapter serves it.

## Decision

- A price book row may carry `provider: <name>` (`ollama`, `azure_openai`, `openai_compat`).
  Rows without it use the default provider, `LFG_PROVIDER`.
- All providers whose settings are present are built at startup: Ollama always (it needs no
  credentials), Azure OpenAI when endpoint and key are both set, the generic adapter when its
  base URL is set. Setting only one of the Azure pair fails fast.
- Routes are resolved once at startup. A row that names a provider which is not configured
  logs a warning and falls back to the default provider, so a laptop with only Ollama can
  still load the shipped price book; the call then fails at the upstream with a clear 502, it
  does not fail at startup.
- `GET /v1/models` reports the serving provider in `owned_by`; `/healthz` lists the configured
  providers; `x_finops.provider`, ledger rows, metrics and spans carry the provider that served
  the call, as before.

Out of scope, still: dynamic routing (fallback on error, load balancing, cost-based choice),
per-team provider overrides, and sending the same model name to two upstreams.

## Consequences

- "Two cloud models and one on-prem" is one process and one base URL for clients.
- The shipped `config/pricing.yaml` annotates every row, so enabling Azure next to Ollama is
  setting two environment variables; nothing else changes.
- Tests and the demo inject providers by name through `create_app(providers=...)`; the single
  `provider=` argument remains the default.
- Per-provider price differences for the same model name (for example an Azure regional
  deployment versus OpenAI direct) still need two price book rows with two names; a
  provider-specific price override is a possible later extension of the same row.
