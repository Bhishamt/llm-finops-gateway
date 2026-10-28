# 0001. Expose an OpenAI-compatible API as the gateway surface

Date: 2026-11-01
Status: Accepted

## Context

Teams already call LLMs through the OpenAI SDKs (Python, JS, LangChain, LlamaIndex) and through
Azure OpenAI, whose wire format is the same. A gateway that requires a new client library would
not be adopted; the cost of switching must be a one-line `base_url` change plus two headers.

The alternatives considered were a bespoke REST API, a gRPC interface, and a sidecar that
intercepts outbound traffic transparently.

## Decision

The gateway speaks the OpenAI Chat Completions API on `POST /v1/chat/completions`, accepts the
common subset of request fields (`model`, `messages`, `temperature`, `max_tokens`), and returns
the standard response shape. Gateway-specific data is added in an extra `x_finops` object that
OpenAI clients ignore. Provider differences (Ollama, Azure OpenAI) are absorbed by adapters
behind a `Provider` protocol; the model name in the request is a logical name resolved by the
adapter (for Azure, to a deployment).

Streaming (`stream: true`) is rejected for now because token usage arrives only at the end of a
stream and the cost path must be correct before it is fast.

## Consequences

- Adoption is a configuration change for existing applications.
- The gateway is limited to what the OpenAI schema expresses; provider-specific features need an
  explicit extension or are out of scope.
- Keeping up with OpenAI schema changes is ongoing work; the gateway should stay on the stable
  subset and reject unknown fields loudly rather than silently dropping them (follow-up).
- A transparent sidecar remains possible later on top of the same core.
