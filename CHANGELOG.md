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

- Streaming: `stream: true` returns OpenAI-format server-sent events; the cost is settled
  from the provider's final usage chunk and delivered in the last event with the `x_finops`
  block. Streams that end without usage are billed at the reservation estimate
  (`usage_estimated: true`); mid-stream upstream failures release the reservation and emit one
  error event (ADR-0012). All adapters implement `stream()`.

- `llm-finops-gateway pricing sync --region <region> [--deployment ...] [--write]`: compares
  the `azure_openai` rows of the price book with the Azure Retail Prices API per deployment
  type and model version, exits non-zero on drift, rewrites the numbers in place with
  `--write` (ADR-0013). Price book rows accept `sku:` and `version:` hints.

- Entra ID authentication for Azure OpenAI: `LFG_AZURE_OPENAI_AUTH=entra` sends bearer tokens
  from `DefaultAzureCredential` (managed identity, service principal or `az login`) instead of
  an API key; tokens are cached and refreshed per request. New optional extra `[azure]`,
  included in the Docker image (ADR-0014).

- OpenAI request surface (ADR-0015): `tools`, `tool_choice`, `parallel_tool_calls`,
  `response_format`, `top_p`, `stop`, `seed`, `user`, penalties and `logprobs` are passed
  through; messages accept content parts, `tool_calls`, `tool_call_id` and the `developer`
  role; responses and streams return `tool_calls`. `max_completion_tokens` is accepted from
  clients. Unknown request fields are rejected with 422. The budget estimate counts content
  parts, tool-call payloads and tool definitions. Ollama translates tools and JSON output.

- Report page `GET /finops` (budgets, chargeback, totals; HTML, no JavaScript, refreshes every
  minute) and `GET /v1/finops/summary.csv` for Excel and Power BI. One access rule for every
  report view: a team key reads its own team, a key with the new `role: finops` reads every
  team, no key is 401 in `api_key` mode; the page has a login form with a session cookie
  (ADR-0016). `keys new --role finops`. Guidance in `docs/reporting.md`.

- Grafana dashboard JSON for the Prometheus metrics (`grafana/llm-finops-gateway.json`) and
  SDK examples in Python and TypeScript (`examples/`): completion with `x_finops`, streaming,
  tool-call round trip, rejected call.

- Currency: `pricing sync --currency EUR` fetches the retail list in that currency and
  `--write` sets the book's `currency:`; the price book validates the code; responses carry
  `x_finops.currency` and `X-FinOps-Currency`. Amounts in `cost_usd`, `monthly_usd` and the
  `*_usd` fields are in the book's currency; the names stay for API stability before 1.0.

- Webhook alerts (`LFG_ALERT_WEBHOOK_URL`, `LFG_ALERT_COOLDOWN_SECONDS`): `budget_warn`,
  `budget_block`, `upstream_error` and `upstream_rate_limited` events as JSON with a Slack-ready
  `text`, once per team/provider per cooldown, delivered in the background (ADR-0017).

- Report page: sortable columns, per-table text filter, team selector (all or a subset,
  totals follow), display currency switch (`LFG_REPORT_RATES`, display only), and the
  effective policy per team and the price book opened from their version links.

- `LFG_ALERT_FORMAT=teams` sends Adaptive Cards for Teams Workflows webhooks;
  `llm-finops-gateway alerts test` sends one sample event. Report page: the team selector is a
  dropdown with checkboxes; sort order survives a reload.

- Screenshots of the report page (ok, warn and block states from real traffic) and the Grafana
  dashboard in the README; the local Grafana allows anonymous viewing; stat panels show 0
  instead of "No data" when a counter is empty. Architecture document and diagram updated
  for providers per model, streaming, Entra ID, reports, alerts and the Azure deployment shape;
  demo chart regenerated.

- `llm-finops-gateway ledger reconcile --resource <id> --day YYYY-MM-DD`: one day of the
  ledger against Azure Cost Management per model, with the billed SKU and the two kinds of gap
  named (traffic that bypassed the gateway, price book SKU mismatch) (ADR-0018). Test plan 4.5
  reconciliation of 2026-10-30 documented.

- `deploy/azure/`: Bicep for Azure Container Apps with a user-assigned managed identity
  (Cognitive Services OpenAI User on the account, AcrPull on the registry), Azure Database
  for PostgreSQL Flexible Server for the ledger (Azure Files + SQLite as the fallback), Log
  Analytics, team key hashes as a secret file; README with costs and the production additions.
- `deploy/azure/`: Key Vault with the PostgreSQL password, the database URL (read by the app
  with its identity) and the raw team keys (`keys.sh`); the template can create the Azure
  OpenAI account and model deployments too (`createAzureOpenAi`); `postgresLocation` and
  `postgresSku` parameters, with the region decision recorded in the README.
- `deploy/azure/db_roles.py`: `lfg_app` (write) and `lfg_reporting` (read-only) PostgreSQL
  roles with passwords and URLs in Key Vault; the gateway stops running as the admin. Bicep
  `databaseUrl` parameter keeps the app-role URL across redeployments. `docs/database.md`
  documents engines, schema, roles and operations.
- `[postgres]` extra (psycopg); the ledger is tested against a real PostgreSQL
  (`tests/test_ledger_postgres.py`, runs when `LFG_TEST_DATABASE_URL` is set); the Docker
  image includes the driver.

### Changed
- An upstream 429 is returned as `429 upstream_rate_limited` with the provider's `Retry-After`
  instead of `502 upstream_error`; the reservation is still released and nothing is recorded.
- `GET /v1/finops/summary` and `/v1/finops/budgets` now require a key in `api_key` mode and
  are scoped to the key's team unless the key has `role: finops`.
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
