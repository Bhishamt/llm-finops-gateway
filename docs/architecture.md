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
│            │                                    │  3. provider.chat(...)       ── 502 │
│            │                                    │     (release reservation)           │
│            │                                    │  4. price_book.cost(tokens)         │
│            │ ◀──────────────────────────────────│  5. ledger.record + settle (1 txn)  │
│            │  OpenAI response + x_finops        │  6. budget_status -> headers, body  │
└────────────┘                                    └──────┬──────────────┬───────────────┘
                                                         │              │
                                              ┌──────────▼───┐   ┌──────▼────────────────┐
                                              │ Provider     │   │ Ledger (SQLAlchemy)   │
                                              │ Ollama       │   │ cost_records          │
                                              │ Azure OpenAI │   │ SQLite / Postgres ... │
                                              └──────────────┘   └───────────────────────┘
```

Order matters: steps 0 to 2 fail **before** any money is spent. Step 3 failures are surfaced as
HTTP 502 with the provider name; nothing is written to the ledger and the budget reservation
is released. Step 2 reserves the call's estimated cost atomically (ADR-0007); step 5 settles
the actual cost and the ledger row in one transaction; step 6 re-reads the counter so the
response reflects the state including this call. Prometheus counters and the OpenTelemetry
span are updated along the way (`metrics.py`, `telemetry.py`).

A rendered version of this flow is in `images/architecture.svg`.

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
| `providers/base.py`         | `Provider` protocol, `CompletionResult`, `ProviderError`                |
| `providers/ollama.py`       | Adapter for Ollama `/api/chat`                                          |
| `providers/azure_openai.py` | Adapter for Azure OpenAI data-plane REST with deployment mapping        |
| `providers/openai_compat.py` | Adapter for any OpenAI Chat Completions server: Azure OpenAI v1, vLLM, LocalAI, OpenAI (ADR-0010) |
| `main.py`                   | `create_app()` factory, endpoints, error handlers, model-to-provider routes (ADR-0004, 0011) |
| `cli.py`                    | `llm-finops-gateway serve` and `keys new | hash | check`                |

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
| `cost_usd`           | numeric(14,8)  | exact decimal                               |
| `price_book_version` | str            | the version used to price this row          |

`budget_counters` (one row per team and UTC month): `spent_usd`, `reserved_usd`. Enforcement
reads this row; reports read `cost_records`.

No prompt or completion text is stored.

## Cost model

`cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000`, computed with
`decimal.Decimal` from string-parsed YAML values so that `0.15` is exactly `0.15`. Prices are
USD per million tokens, matching how providers publish them.

## Deployment shape (target)

```
Azure Container Apps ── llm-finops-gateway ──▶ Azure OpenAI (private endpoint)
        │                     │
   Key Vault (api key)   Azure Database for PostgreSQL (ledger)
                              │
                        Grafana / Power BI (chargeback)
```

The Terraform module for this is on the roadmap. Until then: run the container anywhere
with the `LFG_*` variables set and `LFG_DATABASE_URL` pointing at a persistent database.

## Roadmap mapping

| Capability (roadmap)        | Where it plugs in                                            |
| --------------------------- | ------------------------------------------------------------ |
| Evaluation replay (ADR-0009)| Opt-in sampled capture; `eval run` replays through the gateway and scores quality + cost |
| OIDC attribution            | Third branch in `resolve_attribution`: JWT from Keycloak or Entra, team from a claim |
| Streaming                   | Provider yields chunks; settle on the final usage chunk      |
| PII policy                  | Additional check inside step 2, same policy file             |
| Shared rate limits          | Token bucket row in the database or Redis, replacing the in-memory window |
| JWT attribution (later)     | A third branch in `resolve_attribution`, same `Attribution` value |
| Amortised self-hosted cost  | Price book entries with hourly rate and a throughput model   |
