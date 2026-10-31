# 0018. Reconcile the ledger against the provider's bill, per meter, one day at a time

Date: 2026-10-31
Status: Accepted

## Context

A ledger that nobody checks against the invoice is a belief, not a chargeback. The first
real reconciliation (test plan 4.5, 2026-10-30) was done by hand with the Cost Management
Query API and showed what such a check must separate: tokens the gateway did not see (calls
that bypassed it), and prices the book had wrong (a regional SKU billed while the book carried
the global list price). Both are findings a FinOps practitioner needs weekly, not once.

## Decision

- `llm-finops-gateway ledger reconcile --resource <id> --day YYYY-MM-DD` asks Azure Cost
  Management for that day's meters of one resource (subscription scope, resource filter,
  daily granularity; two queries because the API allows two aggregations each), classifies
  each meter with the `pricing sync` rules (model, input / output / cached, global / regional
  / data zone), converts the quantity to tokens by the meter unit (1K or 1M), and prints one
  line per model: ledger calls, tokens and cost next to Azure tokens and USD cost, plus the
  SKU Azure billed.
- Tokens are the primary check: a positive Azure minus ledger delta is traffic that did not
  pass through the gateway, which is a rollout problem, not a pricing one. Cost is the second
  check: it matches when the book carries the billed SKU's price; otherwise the report names
  the SKU and points at `pricing sync --deployment`.
- Days are UTC on both sides. Authentication is the gateway's Entra identity
  (`DefaultAzureCredential`, management scope), so the same role that reads cost on the
  resource (`Cost Management Reader`) is all it needs; no key, no export pipeline.
- Out of scope: automatic repricing of past ledger rows, other providers' billing APIs, and
  invoice-level (monthly, taxed) totals. Those belong to the enterprise reporting layer or a
  later ADR.

## Consequences

- The ledger can be proven against the bill with one command, daily, and the two kinds of gap
  are named separately in the output.
- Cost Management data lags 8 to 24 hours; the command reports a day, not the current hour.
- The meter classifier is shared with `pricing sync`, so a new meter spelling breaks (and is
  fixed in) one place.
