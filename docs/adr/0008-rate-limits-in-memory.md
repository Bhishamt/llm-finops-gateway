# 0008. Per-team rate limits: sliding window, in memory, per instance

Date: 2026-11-15
Status: Accepted

## Context

Budgets stop monthly overspend; they do nothing against a runaway loop that burns the whole
month in an hour. A request-rate ceiling per team is the usual complement, and the policy file
is the natural place for it.

## Decision

`rate_limit.requests_per_minute` in `policies.yaml`, resolvable per team like budgets. The
engine keeps a 60-second sliding window of timestamps per team, in process memory, and rejects
the N+1th request with HTTP 429, `error: rate_limited`, and a `Retry-After` header computed
from the oldest request in the window. Rate limiting runs after the allow-list and before the
budget reservation, so a rejected burst never touches the counters.

## Consequences

- Correct for a single gateway instance. With several replicas the effective limit is
  `replicas x requests_per_minute`. That is acceptable for the first deployments and is stated
  in the docs; a shared store (Redis, or the database with a token bucket row) is the fix when
  multiple replicas are needed and will be its own ADR.
- Limits are requests, not tokens. Token-rate limits are what providers meter on and would
  need the same estimate as ADR-0007; a follow-up once there is demand.
- State is lost on restart, which only makes the limiter briefly more permissive.
