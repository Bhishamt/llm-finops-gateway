# 0002. A model without a configured price is rejected, not billed at zero

Date: 2026-11-01
Status: Accepted

## Context

A gateway can only attribute cost for models it has a price for. When a request names a model
missing from `config/pricing.yaml` there are three options: forward the call and record zero
cost, forward the call and record "unknown", or refuse the call.

The first option is the common default in logging-style tools and the most harmful: spend becomes
invisible exactly when a new, often expensive, model shows up. The second keeps the data honest
but still lets unpriced spend happen. The FinOps principle at stake is that every unit of cloud
spend has an owner and a price before it is incurred.

## Decision

`PriceBook.cost()` raises `UnknownModelError` for any model not in the price book, and the
gateway checks `PriceBook.has(model)` **before** calling the provider, returning HTTP 422 with
the model name. Adding a model to the price book (and bumping `version`) is the only way to
enable it. `GET /v1/models` therefore doubles as the allow-list.

## Consequences

- New models require a one-line config change and a version bump. That friction is intentional;
  it is the moment to assign a price and, later, a policy.
- Self-hosted models still need an entry, even at `0.00`, which makes the zero explicit and
  reviewable. Amortised GPU cost can replace the zero later without touching callers.
- Ledger rows always carry a non-null, reproducible cost and the price book version used.
- Tests must cover the "reject before upstream call" path to prevent regressions that spend
  money and then fail.
