# 0012. Streaming responses, with the cost settled from the final usage chunk

Date: 2026-10-30
Status: Accepted. Replaces the "streaming is rejected for now" paragraph of ADR-0001.

## Context

Chat applications stream. ADR-0001 rejected `stream: true` because token usage arrives only
at the end of a stream and the cost path had to be right before it was fast. The cost path is
now settled: the budget is reserved before the call (ADR-0007), the price book handles cached
and reasoning tokens, and the providers report usage on the last chunk when asked
(`stream_options: {"include_usage": true}` on Azure OpenAI and OpenAI-compatible servers,
`prompt_eval_count` / `eval_count` on Ollama's final NDJSON line).

Two things are different from a non-streaming call. The cost is not known when the HTTP
headers go out, so it cannot be a response header. And a stream can fail or be abandoned
half-way, after some output was generated and paid for upstream.

## Decision

- `stream: true` returns `text/event-stream` in the OpenAI chunk format: `chat.completion.chunk`
  events with `delta.content`, a chunk with `finish_reason`, then one final chunk with empty
  `choices`, the `usage` object and the gateway's `x_finops` block, then `data: [DONE]`.
  `X-FinOps-Request-Id` is the only FinOps header; cost and budget state travel in the final
  chunk.
- Policy checks and the budget reservation run before the first byte, exactly as for a
  non-streaming call. The ledger row and the settlement are written when the final usage
  arrives, in one transaction.
- If the provider closes the stream without usage, the call is billed at the reservation
  estimate (prompt chars / 4 plus the output bound), the ledger row is written, the final
  chunk carries `usage_estimated: true` and a warning is logged. Fail closed: an unmeasured
  call is never a free call (ADR-0002).
- If the upstream fails mid-stream, the reservation is released, nothing is recorded, and the
  client receives one `data: {"error": ...}` event followed by `[DONE]`. A client that
  disconnects first gets its reservation released as well.
- Adapters expose `stream()` next to `chat()`; the `Provider` protocol requires both.

## Consequences

- OpenAI SDK clients work unchanged with `stream=True`; the gateway can sit in front of a
  chat UI.
- A mid-stream upstream failure is the one case where tokens were generated upstream and not
  attributed. The provider's own bill will show them; the `stream failed` log line with the
  request id is the audit trail. This is accepted for now; recording partial output at the
  estimate is a possible later change.
- Streaming output is not recorded or inspected by the gateway; the PII policy planned for
  requests will apply to the request side only until a streaming filter exists.
