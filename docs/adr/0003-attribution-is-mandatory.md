# 0003. Calls without an owner are rejected, not guessed

Date: 2026-11-01
Status: Accepted

## Context

Chargeback and showback only work when every cost record has an owner. In cloud FinOps the
classic failure is the "unallocated" bucket that grows until nobody can explain the bill. Options
for an LLM gateway: infer the owner from the API key, the client IP or a default tag; record the
call as `unknown`; or refuse calls that do not identify themselves.

Inference ties the gateway to a specific auth mechanism before one exists and is wrong often
enough to poison reports. An `unknown` bucket is the cloud anti-pattern transplanted.

## Decision

Every request must carry `X-Team` and `X-Project`; `X-Use-Case` is optional and defaults to
`unspecified`. Values are validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` so they can be
used as dimension keys in reports and labels in observability back ends. Missing or invalid
headers return HTTP 400 with the list of missing headers and a hint, before any provider call.

Attribution is a FastAPI dependency (`attribution_from_headers`) so that later auth mechanisms
(API keys bound to a team, JWT claims) can supply the same `Attribution` value without changing
the endpoint.

## Consequences

- Clients must set two headers; with the OpenAI SDK this is a `default_headers` argument.
- Reports never contain an unallocated row. If a team sends wrong tags, that is a visible,
  fixable data-quality problem rather than an invisible one.
- The policy engine (ADR-0005) can key budgets and allow-lists on the same tags.
- Per-tenant isolation later needs attribution to be derived from an authenticated identity, not
  trusted from headers; that will be its own ADR.
