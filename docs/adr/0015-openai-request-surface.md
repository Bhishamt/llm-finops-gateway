# 0015. The accepted OpenAI request surface: pass through what we can price, reject the rest loudly

Date: 2026-10-30
Status: Accepted. Closes the "reject unknown fields loudly" follow-up of ADR-0001.

## Context

The first version accepted `model`, `messages`, `temperature`, `max_tokens` and `stream` and
silently dropped everything else. Real clients send more: agents send `tools` and
`tool_choice`, structured-output clients send `response_format`, LangChain sends `top_p`,
`stop`, `seed`, `user` and penalties, gpt-5 clients send `max_completion_tokens`, multimodal
clients send a list of content parts, and every tool-using conversation carries assistant
messages with `tool_calls` and `tool` messages with `tool_call_id`. Dropping those made the
gateway "work" while quietly breaking function calling; rejecting message shapes broke image
prompts with a 422.

None of these fields change how a call is priced: the price book prices tokens, and the
provider's usage counts tool definitions, tool calls and image tokens.

## Decision

- `ChatRequest` lists every accepted field and is `extra="forbid"`: an unknown field is a 422
  naming it, never a silent drop. Accepted and passed through unchanged: `tools`,
  `tool_choice`, `parallel_tool_calls`, `response_format`, `top_p`, `stop`, `seed`, `user`,
  `presence_penalty`, `frequency_penalty`, `logprobs`, `top_logprobs`. Accepted and handled by
  the gateway: `max_tokens`, `max_completion_tokens`, `temperature`, `stream`,
  `stream_options` (usage is always requested). `n` is accepted only as 1.
- Messages carry `content` as a string, a list of content parts or null, plus `name`,
  `tool_calls` and `tool_call_id`; they are forwarded as sent. The `developer` role is accepted.
- Responses return `tool_calls` and `finish_reason: tool_calls` as the provider does; streaming
  forwards `delta.tool_calls` fragments unchanged.
- Adapters receive the pass-through fields as one `extra` mapping. Azure OpenAI and
  OpenAI-compatible servers get them verbatim. Ollama gets what it understands (`tools`,
  `format` for JSON output, `top_p`, `stop`, `seed`) with its tool-call shape translated both
  ways (object arguments on the Ollama side, JSON strings on the OpenAI side); the rest is
  dropped with a debug log, because Ollama would reject or ignore it anyway.
- The budget estimate counts the text of content parts, tool-call payloads and the tool
  definitions, so a large tool schema reserves a proportionally larger amount.

## Consequences

- Function calling, JSON mode and image prompts work through the gateway with the OpenAI SDK
  and LangChain unchanged; the ledger sees exactly what the provider charged.
- A client that sends a field this gateway does not know gets a 422 immediately. That is the
  intended friction: the field is either added to the list (one line) or it was a mistake.
- Audio, image generation and the Responses API remain out of scope; they are separate
  endpoints, not fields.
