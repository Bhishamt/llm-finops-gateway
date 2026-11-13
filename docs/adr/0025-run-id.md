# 0025. A run id groups the calls of one agent or pipeline run

Date: 2026-11-13
Status: Accepted

## Context

The ledger has one row per call with team, project and use case, which answers "who spent
what on what". It does not answer "what did this one agent run cost": a run is a series of
calls that share none of those dimensions and cannot be told apart afterwards. Issue #61
asks for the smallest thing that closes that gap without turning the ledger into a tracing
store.

## Decision

- `X-Run-Id` is optional on `POST /v1/chat/completions` and on the LiteLLM plugin. It is
  opaque: no format, no pattern, no interpretation. The only check is a 128-character bound,
  the width of the column, so an over-long header is a `400 invalid_run_id` before the
  upstream call rather than a database error after it.
- The value is stored on `cost_records.run_id` (nullable) and echoed twice: as
  `X-FinOps-Run-Id` on both the non-streaming and the streaming response, and as `run_id`
  in the `x_finops` block. A call that sends no run id records null and gets no header, so
  existing clients and exports are unchanged.
- The FOCUS export adds `run_id` to `Tags` only when a row has one. An empty key would make
  every existing export differ, and a null tag carries no information.
- The column is not indexed. Nothing in the gateway queries by run id yet; the index belongs
  with the first query that needs it, and adding one later is an `ALTER` like the column.
- This is not a trace id: it does not relate calls to each other, deduplicate retries, or
  carry per-step timings. A run id on a retried call is whatever the caller sends, and the
  ledger keeps both rows, as it does today.

## Consequences

- Spend per run is one `GROUP BY run_id` away, and the run id travels with the row into the
  FOCUS dataset without a second store.
- Existing deployments add the column by hand (`docs/database.md`); rows written before it
  stay null, and a gateway that starts without it fails at the first insert, which
  `docs/database.md` warns about.
- Callers that leak sensitive data into the header would leak it into the ledger and the
  export. The gateway does not interpret the value, so it cannot redact part of it; the
  documentation says the header is not for secrets.
