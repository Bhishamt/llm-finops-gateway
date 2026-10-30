# 0010. One generic adapter for every OpenAI-compatible endpoint

Date: 2026-10-29
Status: Accepted

## Context

The gateway started with two adapters: Ollama (`/api/chat`) and Azure OpenAI (the
deployment-scoped data-plane path with `api-version`). Most other places a team would send
traffic already speak the OpenAI Chat Completions wire format: OpenAI itself, the Azure OpenAI
v1 endpoint exposed by Azure AI Foundry resources (`https://<resource>.openai.azure.com/openai/v1`),
vLLM, LocalAI, TGI, and Ollama's own `/v1`. Writing one adapter per vendor would copy the same
forty lines with a different base URL and header name.

Two things do differ between these servers: how the caller authenticates (bearer token, a
vendor header, or nothing on a trusted network), and what the upstream calls the model (an
Azure deployment name, a vLLM `--served-model-name`). Both are configuration, not code.

## Decision

Add `providers/openai_compat.py`, selected with `LFG_PROVIDER=openai_compat` and configured by
four settings: `LFG_OPENAI_COMPAT_BASE_URL` (required; the path up to and including `/v1`),
`LFG_OPENAI_COMPAT_API_KEY` (optional; sent as `Authorization: Bearer`),
`LFG_OPENAI_COMPAT_HEADERS` (optional JSON map of extra headers, applied after the bearer header
so a vendor header can replace it), and `LFG_OPENAI_COMPAT_MODELS` (optional JSON map from the
model name clients and the price book use to the upstream name).

The adapter posts `{"model", "messages", "stream": false, "temperature"?, "max_tokens"?}` to
`{base_url}/chat/completions` and reads `choices[0].message.content` and `usage`. It reports
itself as provider `openai_compat` on ledger rows, metrics and spans.

The existing `azure_openai` adapter stays for the classic deployment path and for the
`api-key` header; it is not deprecated. The price book remains keyed by the client-facing model
name, so the same `pricing.yaml` works whichever adapter serves a model (ADR-0002).

## Consequences

- Any OpenAI-compatible server is a configuration change, no code. Azure AI Foundry resources
  can be reached either way: `azure_openai` for the deployment path, `openai_compat` for v1.
- One gateway instance still serves one provider. Routing by model to several upstreams is a
  separate decision and stays out of scope (ADR-0001).
- Parameter quirks of specific model families (for example servers that reject `max_tokens`
  in favour of `max_completion_tokens`, or fix `temperature`) are not handled here; the
  upstream error is returned as HTTP 502 with the provider's message, and a follow-up can add
  per-model overrides when a real deployment needs them.
- Streaming is still rejected before the adapter is reached (ADR-0001).
