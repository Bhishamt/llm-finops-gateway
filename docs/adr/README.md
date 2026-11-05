# Architecture decision records

Each significant design decision is recorded here as a short, numbered Markdown file. ADRs are
immutable once accepted; a later ADR supersedes an earlier one rather than editing it.

| ADR | Title | Status |
| --- | ----- | ------ |
| [0001](0001-openai-compatible-proxy.md) | Expose an OpenAI-compatible API as the gateway surface | Accepted, streaming paragraph replaced by 0012 |
| [0002](0002-unknown-model-is-an-error.md) | A model without a configured price is rejected, not billed at zero | Accepted |
| [0003](0003-attribution-is-mandatory.md) | Calls without an owner are rejected, not guessed | Accepted |
| [0004](0004-app-factory-and-dependency-injection.md) | Application factory with injected price book, ledger and provider | Accepted |
| [0005](0005-policy-as-code-budgets.md) | Policy as code: per-team budgets with warn and block thresholds | Accepted, enforcement mechanism replaced by 0007 |
| [0006](0006-team-bound-api-keys.md) | Team-bound API keys; headers only narrow, never override | Accepted |
| [0007](0007-atomic-budget-reservation.md) | Atomic budget reservation before the upstream call | Accepted |
| [0008](0008-rate-limits-in-memory.md) | Per-team rate limits: sliding window, in memory, per instance | Accepted |
| [0009](0009-evaluation-replay.md) | Evaluation replay: re-run recorded traffic to score quality and cost together | Proposed |
| [0010](0010-generic-openai-compatible-provider.md) | One generic adapter for every OpenAI-compatible endpoint | Accepted |
| [0011](0011-provider-per-model.md) | Several providers in one gateway, chosen per model by the price book | Accepted |
| [0012](0012-streaming-settled-on-final-usage.md) | Streaming responses, with the cost settled from the final usage chunk | Accepted |
| [0013](0013-price-book-sync-not-live-lookup.md) | Price book sync from the Azure Retail Prices API, never a live lookup | Accepted |
| [0014](0014-entra-id-auth-for-azure-openai.md) | Azure OpenAI with Entra ID: the gateway calls with its own identity | Accepted |
| [0015](0015-openai-request-surface.md) | The accepted OpenAI request surface: pass through what we can price, reject the rest loudly | Accepted |
| [0016](0016-report-page-and-scoped-access.md) | A built-in report page, and who may read what | Accepted |
| [0017](0017-webhook-alerts.md) | Webhook alerts for budget and upstream events, sent by the gateway itself | Accepted |
| [0018](0018-ledger-reconcile.md) | Reconcile the ledger against the provider's bill, per meter, one day at a time | Accepted |
| [0019](0019-litellm-plugin.md) | The FinOps rules as a LiteLLM plugin | Accepted |
| [0020](0020-open-core-boundary.md) | What stays in this repository and what an enterprise layer adds | Accepted |
| [0021](0021-project-registry-and-budgets.md) | Projects and use cases are registered in the policy file, with their own budgets | Proposed |
| [0022](0022-configuration-hot-reload.md) | The price book and the policy file are reloaded when they change | Proposed |
| [0023](0023-focus-export.md) | The ledger is exported in the FOCUS format | Proposed |
| [0024](0024-litellm-dashboard-plugin.md) | The report page runs inside the LiteLLM dashboard as a plugin | Proposed |

## Template

```markdown
# NNNN. Title

Date: YYYY-MM-DD
Status: Proposed | Accepted | Superseded by NNNN | Deprecated

## Context
What problem are we solving and what forces are at play.

## Decision
What we decided, in one or two paragraphs.

## Consequences
What becomes easier, what becomes harder, what we must do as a result.
```
