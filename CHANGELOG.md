# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Until 1.0, minor versions may change the API; this
is called out here.

## [Unreleased]

### Added
- Generic OpenAI-compatible provider (`LFG_PROVIDER=openai_compat`): base URL, optional bearer
  key, extra headers and a model name map. Covers the Azure OpenAI v1 endpoint of Azure AI
  Foundry resources, vLLM, LocalAI and OpenAI (ADR-0010).
- Several providers in one gateway: every configured provider is built at startup and a price
  book row's `provider` key says which one serves the model; `LFG_PROVIDER` is the default.
  `GET /v1/models` reports the serving provider, `/healthz` lists the configured ones (ADR-0011).
- Prompt-cache pricing: `cached_input` in the price book, `usage.prompt_tokens_details.cached_tokens`
  read from Azure OpenAI and OpenAI-compatible responses, billed at the cached price, recorded
  on the ledger row and echoed in the response `usage`.

- Reasoning models: a price book row with `reasoning: true` (gpt-5, gpt-5-mini, gpt-5-nano
  shipped) makes the gateway send `max_completion_tokens` instead of `max_tokens` and drop the
  temperature; adapters accept `max_completion_tokens`. `usage.completion_tokens_details.reasoning_tokens`
  is passed through when the provider reports it.

### Changed
- An upstream 429 is returned as `429 upstream_rate_limited` with the provider's `Retry-After`
  instead of `502 upstream_error`; the reservation is still released and nothing is recorded.
- Price book version `2026-11-02`: cached-input prices for the GPT-4o and GPT-4.1 rows and a
  `provider` on every row.

## [0.1.0] - 2026-10-28

First public release.

### Added
- OpenAI-compatible `POST /v1/chat/completions` (non-streaming) in front of Azure OpenAI or
  Ollama (ADR-0001).
- Versioned price book (`config/pricing.yaml`); a model without a price is rejected with
  HTTP 422 before any upstream call (ADR-0002).
- Mandatory attribution: `X-Project` always, the team from a team-bound API key
  (`Authorization: Bearer lfg_...`, SHA-256 hashes in `config/keys.yaml`, `LFG_AUTH_MODE=api_key`,
  default) or from `X-Team` in `LFG_AUTH_MODE=headers` for development. Keys can be restricted
  to projects, expire or be disabled; `key_id` is recorded on every ledger row (ADR-0003, 0006).
  CLI: `llm-finops-gateway keys new | hash | check`.
- Policy as code (`config/policies.yaml`): per-team model allow-lists, monthly budgets with a
  warning threshold (default 80 %) and a blocking threshold (default 100 %), warn-only mode,
  and requests-per-minute limits with HTTP 429 and `Retry-After` (ADR-0005, 0008).
- Atomic budget reservation: the estimated cost of a call is reserved with one conditional
  update before the upstream call, settled with the actual cost in the same transaction as the
  ledger row, released on upstream failure (ADR-0007).
- Budget state on every response: `x_finops` block, `X-FinOps-Budget-*`, `X-FinOps-Warning`,
  `X-FinOps-Cost-USD`, `X-FinOps-Request-Id` headers; structured 401/403/422/429 bodies;
  `WARNING` log lines on warn and block.
- SQLite (any SQLAlchemy URL) ledger with one row per call including the price book version;
  `GET /v1/finops/summary` chargeback and `GET /v1/finops/budgets` status endpoints;
  `GET /v1/models`; `GET /healthz`.
- Prometheus `GET /metrics` and optional OpenTelemetry tracing (`[otel]` extra) with
  `finops.*` span attributes, OTLP to any backend including Langfuse.
- Application factory with injected collaborators (ADR-0004); typed Python, `mypy --strict`,
  111 tests.
- `demo/run_demo.py`: 300-call simulation with no keys, or live mode against a running
  gateway; Markdown report, JSON summary, chargeback chart.
- Docker image (non-root, healthcheck), docker-compose with Ollama, GitHub Actions CI on
  Python 3.11 and 3.12 with `pip-audit` and a Docker build, release workflow, pre-commit,
  Dependabot, CLA bot.
- Documentation: README, architecture, deployment guide, Azure setup, manual test plan with
  results, comparison with existing gateways, ADRs 0001 to 0009, CONTRIBUTING, GOVERNANCE,
  CODE_OF_CONDUCT, SECURITY, CLA, MAINTAINERS.

[Unreleased]: https://github.com/andrasKelle/llm-finops-gateway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/andrasKelle/llm-finops-gateway/releases/tag/v0.1.0
