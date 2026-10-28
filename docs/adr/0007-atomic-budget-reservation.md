# 0007. Atomic budget reservation before the upstream call

Date: 2026-11-15
Status: Accepted (replaces the read-then-act check described as a limitation in ADR-0005)

## Context

ADR-0005 enforced budgets by summing the ledger and comparing before the call. Two concurrent
calls at 99 % both passed, and the sum grew with the ledger. Both were documented as
limitations. Blocking at a budget is only worth something if it holds under load.

The cost of a call is not known until the provider answers. Enforcing a hard limit therefore
needs either a pessimistic estimate reserved up front, or acceptance of overshoot by one call
per concurrent caller. We chose the estimate.

## Decision

A `budget_counters` table keyed by `(team, month)` holds `spent_usd` and `reserved_usd`.

1. **Estimate.** Before the call, estimate the cost from the price book: input tokens as
   prompt characters / 4 plus 8 per message, output tokens as `max_tokens` or the policy's
   `reserve_output_tokens` (default 1024). Deliberately on the high side.
2. **Reserve atomically.** One conditional `UPDATE ... SET reserved = reserved + :est WHERE
   spent + reserved + :est <= :limit`. Row count 0 means the budget is exhausted; nothing was
   reserved and the call is rejected with 403 `budget_exceeded`, the estimate in the message.
   A team whose committed spend is already at or above the limit is rejected even for a
   zero-cost estimate (free local models do not get a pass once the budget is gone).
3. **Settle.** After the provider answers, the ledger row insert and the counter update
   (`spent += actual`, `reserved -= estimate`) happen in the same transaction.
4. **Release.** If the provider call raises, the reservation is returned.

Warn-only teams reserve without a limit, so their status still reflects in-flight spend but
nothing is ever rejected. Status and headers report `spent`, `reserved` and `used_pct` on the
committed total (`spent + reserved`).

The ledger `SUM` stays available for reports; enforcement reads the counter, which is O(1).

## Consequences

- Concurrency is correct by construction: the database serialises the conditional update.
  Tested with 20 threads against a 10-call limit.
- A burst of large reservations near the limit can reject calls that would have fit on
  actual cost. The estimate is tunable per team (`reserve_output_tokens`), and
  `max_tokens` in the request tightens it. That trade-off is the price of a hard stop.
- The counter is denormalised state. If it ever drifts from the ledger (manual edits, crashes
  between reserve and settle), a reconciliation job rebuilding counters from `cost_records`
  fixes it; a crash leaves a reservation in place until that job runs. Planned as a CLI
  command.
- Works on SQLite and PostgreSQL unchanged; the floor-at-zero uses `CASE`, not `GREATEST`.
