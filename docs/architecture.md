# Architecture

## Request path

```
┌────────────┐  POST /v1/chat/completions         ┌─────────────────────────────────────┐
│  client    │  + X-Team / X-Project / X-Use-Case │  llm-finops-gateway (FastAPI)       │
│ (OpenAI    │ ──────────────────────────────────▶│                                     │
│  SDK etc.) │                                    │  0. resolve_attribution      ── 401 │
│            │  + Authorization: Bearer lfg_...   │     key -> team; X-Project    ── 400 │
│            │                                    │  1. price_book.has(model)    ── 422 │
│            │                                    │  2. policy: allow-list       ── 403 │
│            │                                    │     rate limit               ── 429 │
│            │                                    │     reserve(estimate)        ── 403 │
│            │                                    │  3. provider.chat / .stream  ── 502 │
│            │                                    │     (release reservation)    ── 429 │
│            │                                    │  4. price_book.cost(tokens)         │
│            │ ◀──────────────────────────────────│  5. ledger.record + settle (1 txn)  │
│            │  OpenAI response + x_finops,       │  6. budget_status -> headers, body  │
│            │  or SSE with x_finops at the end   │     alerts on warn / block          │
└────────────┘                                    └──────┬──────────────┬───────────────┘
                                                         │              │
                                      ┌──────────────────▼───┐   ┌──────▼────────────────┐
                                      │ Providers (per model)│   │ Ledger (SQLAlchemy)   │
                                      │ Ollama               │   │ cost_records          │
                                      │ Azure OpenAI         │   │ budget_counters       │
                                      │ (key or Entra ID)    │   │ SQLite / Postgres ... │
                                      │ any OpenAI-compatible│   └───────────────────────┘
                                      └──────────────────────┘
```

Order matters: steps 0 to 2 fail **before** any money is spent. Step 3 failures are surfaced as
HTTP 502 with the provider name (an upstream 429 as 429 with `Retry-After`); nothing is written
to the ledger and the budget reservation is released. The provider for a model comes from the
price book row (`provider:`), the default from `LFG_PROVIDER` (ADR-0011). With `stream: true`
step 3 streams chunks to the client and steps 4 to 6 run on the final usage chunk (ADR-0012). Step 2 reserves the call's estimated cost atomically (ADR-0007); step 5 settles
the actual cost and the ledger row in one transaction; step 6 re-reads the counter so the
response reflects the state including this call. Prometheus counters and the OpenTelemetry
span are updated along the way (`metrics.py`, `telemetry.py`).

A rendered version of this flow is in `images/architecture.svg`. Around the request path:
`/finops` (HTML), `/v1/finops/summary(.csv)` and `/v1/finops/budgets` read the ledger behind one
access rule (ADR-0016); `/metrics` and OpenTelemetry spans are written along the way; webhook
alerts fire on budget warn/block and upstream failures (ADR-0017); `pricing sync` keeps the price
book at Azure's list prices offline (ADR-0013).

## Modules

| Module                      | Responsibility                                                          |
| --------------------------- | ----------------------------------------------------------------------- |
| `config.py`                 | `Settings` from `LFG_*` env vars; validates provider credentials early  |
| `models.py`                 | Pydantic request/response schemas (OpenAI shape + `x_finops`)           |
| `auth.py`                   | `KeyRing`, SHA-256 key hashes, `generate_key()` (ADR-0006)              |
| `attribution.py`            | Key or header resolution into an `Attribution` value object (ADR-0003, 0006) |
| `pricing.py`                | `PriceBook` loaded from versioned YAML; exact `Decimal` cost (ADR-0002) |
| `policy.py`                 | `PolicySet` from YAML, `PolicyEngine.check()`: allow-list, rate limit, reservation (ADR-0005, 0007, 0008) |
| `metrics.py`                | Prometheus registry per app instance, `/metrics`                        |
| `telemetry.py`              | Optional OpenTelemetry provider, `finops.*` span attributes             |
| `ledger.py`                 | `CostRecord` and `BudgetCounter` tables; record/settle, reserve, release, summary |
| `providers/base.py`         | `Provider` protocol (`chat`, `stream`), `CompletionResult`, `StreamChunk`, `ProviderError`, OpenAI usage/SSE mapping |
| `providers/ollama.py`       | Adapter for Ollama `/api/chat`                                          |
| `providers/azure_openai.py` | Adapter for Azure OpenAI data-plane REST with deployment mapping; API key or Entra ID token (`providers/azure_identity.py`, ADR-0014) |
| `providers/openai_compat.py` | Adapter for any OpenAI Chat Completions server: Azure OpenAI v1, vLLM, LocalAI, OpenAI (ADR-0010) |
| `pricing_sync.py`           | `pricing sync`: Azure Retail Prices meters matched to price book rows, report and in-place rewrite (ADR-0013) |
| `report.py`                 | `/finops` HTML page, CSV summary, report access scope (ADR-0016) |
| `reconcile.py`              | `ledger reconcile`: one day of the ledger against Azure Cost Management per model (ADR-0018) |
| `alerts.py`                 | Webhook alerts for budget warn/block and upstream failures, cooldown, background delivery (ADR-0017) |
| `main.py`                   | `create_app()` factory, endpoints, error handlers, model-to-provider routes (ADR-0004, 0011) |
| `cli.py`                    | `llm-finops-gateway serve`, `keys new | hash | check`, `pricing sync`, `alerts test` |

## Data model

`cost_records` (one row per successful completion):

| Column               | Type           | Notes                                       |
| -------------------- | -------------- | ------------------------------------------- |
| `id`                 | int, PK        |                                             |
| `ts`                 | datetime (UTC) | indexed                                     |
| `request_id`         | str, unique    | returned to the client as `id`              |
| `team`, `project`    | str, indexed   | attribution dimensions                      |
| `use_case`           | str            |                                             |
| `key_id`             | str, nullable  | which team key was used (ADR-0006)          |
| `provider`, `model`  | str            | `model` indexed                             |
| `input_tokens`, `output_tokens` | int |                                             |
| `cached_input_tokens` | int           | part of `input_tokens` served from the provider's prompt cache |
| `cost_usd`           | numeric(14,8)  | exact decimal, in the price book's currency |
| `price_book_version` | str            | the version used to price this row          |

`budget_counters` (one row per team and UTC month): `spent_usd`, `reserved_usd`. Enforcement
reads this row; reports read `cost_records`.

No prompt or completion text is stored. Engines, the full column list, the PostgreSQL roles
(`lfgadmin` owns, `lfg_app` writes, `lfg_reporting` reads) and operations are in
[`database.md`](database.md).

## Cost model

`cost = ((input_tokens - cached) * input_price + cached * cached_input_price + output_tokens *
output_price) / 1_000_000`, computed with `decimal.Decimal` from string-parsed YAML values so
that `0.15` is exactly `0.15`. Prices are per million tokens in the book's `currency`, matching
how providers publish them; reasoning tokens are part of `output_tokens`. A row's `reasoning:
true` switches the request to `max_completion_tokens`; `provider:` picks the adapter.

## Deployment shape (target)

```
Azure Container Apps ── llm-finops-gateway ──▶ Azure OpenAI (private endpoint)
   managed identity         │   │                 role: Cognitive Services OpenAI User
   (no provider key)        │   └── /finops, /v1/finops/* ──▶ any BI tool (chargeback)
   Key Vault (secrets)      │
                     Azure Database for PostgreSQL (ledger; app writes as lfg_app, readers use lfg_reporting)
   /metrics ──▶ Azure Monitor managed Prometheus ──▶ Azure Managed Grafana (dashboard)
   alerts  ──▶ Slack / Teams webhook
```

`deploy/azure/main.bicep` creates the Container App, identity, role assignments, the
PostgreSQL Flexible Server ledger (or Azure Files + SQLite) and Log Analytics; Entra ID in
front of `/finops`, private networking and Key Vault are the documented next additions. `docs/reporting.md` maps each view to its Azure
service.

## Roadmap mapping

| Capability (roadmap)        | Where it plugs in                                            |
| --------------------------- | ------------------------------------------------------------ |
| Evaluation replay (ADR-0009)| Opt-in sampled capture; `eval run` replays through the gateway and scores quality + cost |
| OIDC attribution            | Third branch in `resolve_attribution`: JWT from Keycloak or Entra, team from a claim |
| PII policy                  | Additional check inside step 2, same policy file             |
| Shared rate limits          | Token bucket row in the database or Redis, replacing the in-memory window |
| Amortised self-hosted cost  | Price book entries with hourly rate and a throughput model   |
