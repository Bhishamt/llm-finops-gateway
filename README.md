# llm-finops-gateway

[![CI](https://github.com/andrasKelle/llm-finops-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/andrasKelle/llm-finops-gateway/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

**An OpenAI-compatible gateway that gives every LLM call an owner, a price and a policy.**

Teams adopt LLMs faster than finance can allocate the bill and faster than security can enforce
policy. This gateway sits between your applications and the model provider (Azure OpenAI,
Ollama, or any OpenAI-compatible endpoint) and, on every call:

1. **Knows who is calling**: team-bound API keys (`Authorization: Bearer lfg_...`), hashes in
   git, the raw key only in the team's secret store. The team cannot be spoofed with a header.
2. **Attributes cost** to a team, project and use case, priced from a versioned price book, and
   writes a ledger row you can charge back from.
3. **Enforces policy as code**: per-team model allow-lists, requests-per-minute limits, and
   monthly budgets with a warning threshold (default 80 %) and a blocking threshold (default
   100 %), declared in YAML and reviewed in pull requests. Budgets are enforced with an atomic
   reservation, so concurrent calls cannot slip past the limit.
4. **Reports and observes**: chargeback and budget endpoints, Prometheus `/metrics`, and
   OpenTelemetry spans with `finops.*` attributes (Grafana, Azure Monitor, Langfuse).

Four rules are built in and intentional (see the [ADRs](docs/adr/)):

- A model without a configured price is **rejected**, not billed at zero.
- A call without an owner (key or `X-Team`, plus `X-Project`) is **rejected**, not guessed.
- Budgets **warn before they block**, and both states are visible in headers, body, logs and
  traces.
- The budget is **reserved before the call**, settled after, released on failure.

![Request path](docs/images/architecture.svg)

## What it looks like

A 300-call simulation across five teams (`python demo/run_demo.py`, no API keys needed):

![Demo chargeback chart](docs/images/demo-chargeback.png)

| team | ok | warned | blocked (budget) | blocked (model) | rate limited | unpriced model | no owner | spend USD | budget used |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| customer-support | 51 | 21 | 27 | 3 | 0 | 0 | 0 | 0.0250 | 100.1% (block) |
| marketing | 30 | 0 | 0 | 0 | 9 | 5 | 0 | 0.0096 | 31.9% (ok) |
| platform | 62 | 16 | 2 | 0 | 0 | 0 | 0 | 0.1472 | 98.1% (warn) |
| research | 14 | 40 | 0 | 0 | 0 | 0 | 0 | 0.2064 | 206.4% (warn, warn-only) |
| shadow-it | 6 | 0 | 0 | 0 | 0 | 0 | 14 | 0.0021 | 21.0% (ok) |

Unallocated spend: **0.00 USD**, by construction.

Against a real bill: on 2026-10-30 the gateway ran 983 calls on an Azure OpenAI deployment.
The next day Azure Cost Management showed 65,870 tokens and 0.096501 USD for the resource; the
ledger had 65,096 tokens and, priced at the deployment's SKU, 0.095241 USD. The difference is
774 tokens from direct API tests that bypassed the gateway. The only other gap was a price
book mismatch (regional SKU billed, global price in the book, 0.0123 USD), which
`pricing sync --deployment regional` reports. Details in
[docs/testing-plan.md](docs/testing-plan.md#reconciliation-of-2026-10-30-against-azure-cost-management-run-2026-10-31).

## Why not LiteLLM, Portkey or an API-management gateway?

Those are good routing gateways and you may already run one. This project is deliberately
narrower and FinOps-first. The differences that matter for a finance or platform team:

| | llm-finops-gateway | Typical LLM gateways (LiteLLM, Portkey, Bifrost, Cloudflare AI Gateway) |
| --- | --- | --- |
| Cost owner | Mandatory `team` / `project` / `use_case` tags; untagged calls are rejected | Spend tied to an API key or "virtual key"; mapping keys to cost centres is manual |
| Price list | Versioned YAML in git; every ledger row records the version it was priced with, so last quarter's chargeback can be recomputed | Built-in price table updated with the software; historical cost is whatever was computed at the time |
| Unknown model | Rejected before the upstream call | Usually forwarded and logged with zero or missing cost |
| Who is calling | Team-bound keys, hashes in git, `key_id` on every ledger row and trace | Virtual keys in a database or admin UI |
| Budgets | Per team, warn and block thresholds, warn-only mode, atomic reservation, declared in YAML and reviewed in PRs | Per key, hard cap, configured in an admin UI or API |
| Evaluation | Replay and regression scoring on recorded calls (roadmap) | Out of scope; separate observability product |
| Routing, caching, fallbacks | Out of scope; run this behind a router if you need them | Core feature |

A longer comparison with sources is in [docs/comparison.md](docs/comparison.md).

## Quick start

```bash
git clone https://github.com/andrasKelle/llm-finops-gateway
cd llm-finops-gateway
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,demo]"
python demo/run_demo.py          # simulated traffic, no keys, writes demo/out/
```

Run `llm-finops-gateway serve` from the repository root (or set `LFG_*_PATH`): the price book,
policies and key file are read from `config/` relative to the working directory.

### Local models with Ollama (no keys, headers mode)

```bash
ollama pull llama3.2:3b
LFG_AUTH_MODE=headers llm-finops-gateway serve --port 8080
```

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Team: platform" -H "X-Project: demo" -H "X-Use-Case: smoke-test" \
  -d '{"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Say hi."}]}' | jq
```

### Team keys (the default, `LFG_AUTH_MODE=api_key`)

```bash
llm-finops-gateway keys new --team platform --id platform-copilot
# prints the key once (lfg_...) and a YAML entry; paste the entry into config/keys.yaml
llm-finops-gateway serve --port 8080
curl -s http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer lfg_..." -H "X-Project: demo" \
  -H "Content-Type: application/json" \
  -d '{"model": "llama3.2:3b", "messages": [{"role": "user", "content": "Say hi."}]}' | jq .x_finops
```

The team comes from the key; an `X-Team` header, if sent, must match. Keys can be restricted
to projects, given an expiry, and disabled. Only SHA-256 hashes are stored (ADR-0006).
Rollout, rotation and the reference Azure deployment are in
[docs/deployment.md](docs/deployment.md).

### Azure OpenAI

Create a `.env` (never commit it; see `.env.example`):

```dotenv
LFG_PROVIDER=azure_openai
LFG_AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
LFG_AZURE_OPENAI_API_KEY=<key from Azure portal or Key Vault>
LFG_AZURE_OPENAI_API_VERSION=2024-10-21
# model name used by clients -> Azure deployment name
LFG_AZURE_OPENAI_DEPLOYMENTS={"gpt-4o-mini": "gpt-4o-mini-eastus"}
```

Then run the gateway and call it with `"model": "gpt-4o-mini"`. Every model name you use must
exist in `config/pricing.yaml`. A step-by-step `az` CLI walkthrough for a disposable test
setup is in [docs/azure-setup.md](docs/azure-setup.md).

Without a key, with the gateway's own identity (ADR-0014): `pip install "llm-finops-gateway[azure]"`,
set `LFG_AZURE_OPENAI_AUTH=entra`, drop `LFG_AZURE_OPENAI_API_KEY`, and give the identity the
`Cognitive Services OpenAI User` role on the resource. `DefaultAzureCredential` picks the
Container App's managed identity in Azure and your `az login` session on a laptop.

### Any OpenAI-compatible endpoint (Azure OpenAI v1, vLLM, LocalAI, OpenAI)

One adapter for every server that speaks the OpenAI Chat Completions API (ADR-0010). The base
URL includes the API prefix; the key is sent as a bearer token; an optional map translates the
model name clients and the price book use into the upstream name (an Azure deployment, a vLLM
served model name):

```dotenv
LFG_PROVIDER=openai_compat
LFG_OPENAI_COMPAT_BASE_URL=https://<resource>.openai.azure.com/openai/v1
LFG_OPENAI_COMPAT_API_KEY=<key>
LFG_OPENAI_COMPAT_MODELS={"gpt-4o-mini": "gpt-4o-mini-eastus"}
# LFG_OPENAI_COMPAT_HEADERS={"X-Custom": "value"}      # extra headers, optional
```

For an Azure AI Foundry resource this is the v1 endpoint, where the deployment name is the model.
The classic deployment path with `api-version` stays available as `LFG_PROVIDER=azure_openai`.
Local servers without authentication (vLLM, LocalAI on a trusted network) simply omit the key.

### Several providers in one gateway

`LFG_PROVIDER` is only the default. Every provider whose settings are present is available at
the same time, and a price book row says which one serves the model (ADR-0011):

```yaml
models:
  gpt-4.1-mini: { input: 0.40, output: 1.60, cached_input: 0.10, provider: azure_openai }
  "llama3.2:3b": { input: 0.00, output: 0.00, provider: ollama }
```

With the shipped price book, setting the two Azure variables next to a local Ollama gives
"cloud models on Azure, small models on-prem" behind one base URL. `GET /v1/models` shows which
provider serves each model. A row naming a provider that is not configured falls back to the
default with a startup warning. `cached_input` prices the prompt tokens the provider served
from its cache (`usage.prompt_tokens_details.cached_tokens`), the way Azure and OpenAI bill them.
`reasoning: true` marks the gpt-5 and o-series rows: the gateway sends `max_completion_tokens`
instead of `max_tokens` and drops the temperature, which those models reject, so OpenAI SDK
defaults keep working. Reasoning tokens are part of `completion_tokens`, billed as output and
reported in `usage.completion_tokens_details.reasoning_tokens`; a small `max_tokens` can be used
up entirely by reasoning and return an empty message with `finish_reason: length`.

### The response

Standard OpenAI shape plus an `x_finops` block, and `X-FinOps-*` headers:

```json
{
  "id": "gw-7f3a…", "object": "chat.completion", "model": "gpt-4o-mini",
  "choices": [{ "index": 0, "message": { "role": "assistant", "content": "Hi!" }, "finish_reason": "stop" }],
  "usage": { "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15 },
  "x_finops": {
    "cost_usd": "0.0000036", "currency": "USD", "price_book_version": "2026-11-01",
    "team": "platform", "project": "demo", "use_case": "smoke-test", "provider": "azure_openai",
    "key_id": "platform-copilot",
    "policy": {
      "version": "2026-11-01",
      "budget": { "limit_usd": "500", "spent_usd": "412.10", "reserved_usd": "0", "used_pct": "82.4",
                  "remaining_usd": "87.90", "level": "warn", "enforcement": "block",
                  "warn_at_pct": "80", "block_at_pct": "100" },
      "warnings": ["team 'platform' has used 82.4% of its monthly budget (412.10 spent + 0 in flight of 500 USD); calls will be blocked at 100%"]
    }
  }
}
```

```
X-FinOps-Cost-USD: 0.0000036
X-FinOps-Currency: USD
X-FinOps-Budget-Limit-USD: 500
X-FinOps-Budget-Spent-USD: 412.10
X-FinOps-Budget-Used-Pct: 82.4
X-FinOps-Budget-Status: warn
X-FinOps-Warning: team 'platform' has used 82.4% of its monthly budget (412.10 spent + 0 in flight of 500 USD); calls will be blocked at 100%
X-FinOps-Request-Id: gw-7f3a…
```

When the estimated cost of the call does not fit the budget, it is refused before any
provider call:

```json
HTTP 403
{ "detail": { "error": "budget_exceeded",
              "message": "team 'platform' has used 99.8% of its monthly budget (499.10 spent + 0 in flight of 500 USD); this call was estimated at 1.02 USD",
              "team": "platform", "project": "demo", "model": "gpt-4o-mini",
              "policy_version": "2026-11-01",
              "budget": { "level": "block", "used_pct": "99.8", "remaining_usd": "0.90", "...": "..." } } }
```

### Keeping the price book honest: `pricing sync`

```bash
llm-finops-gateway pricing sync --region swedencentral            # report, exit 1 on drift
llm-finops-gateway pricing sync --region swedencentral --write    # rewrite numbers, bump version
```

The command downloads the region's meters from the public Azure Retail Prices API, matches the
rows marked `provider: azure_openai` by model, token kind and deployment type, and prints one
line per price: `ok`, `differs`, `missing`, `unmatched` or `ambiguous`. `--currency EUR` asks
for the list in euros (Microsoft's own conversion, the one on the invoice) and, with `--write`,
sets the book's `currency:` so every ledger row, header and report carries the same unit. Azure bills the same
model differently per deployment type (regional Standard is about 21 % above Global Standard)
and sometimes per model version; a row can say `sku: regional` or `version: 2024-11-20` to pin
those. Nothing is looked up at request time: the price book stays a reviewed file, and
`--write` produces the diff for a pull request (ADR-0013).

### What of the OpenAI API is accepted

Everything an agent or a structured-output client sends is passed through to the provider:
`tools`, `tool_choice`, `parallel_tool_calls`, `response_format`, `top_p`, `stop`, `seed`,
`user`, penalties, `logprobs`; messages may carry content parts (text, images), `tool_calls`
and `tool_call_id`, and responses return `tool_calls` with `finish_reason: tool_calls`, also
while streaming. `max_tokens` and `max_completion_tokens` are both accepted. A field the
gateway does not know is a `422` naming it, never a silent drop (ADR-0015). Ollama gets the
subset it understands, with its tool-call shape translated.

### Proving the ledger against the bill: `ledger reconcile`

```bash
llm-finops-gateway ledger reconcile --resource /subscriptions/.../accounts/<aoai> --day 2026-10-30
```

Asks Azure Cost Management for that day's token meters of the resource (your Entra identity,
`Cost Management Reader` is enough) and prints, per model, the ledger's calls, tokens and cost
next to Azure's tokens and USD cost and the SKU Azure billed. A positive token delta is traffic
that bypassed the gateway; a cost delta with matching tokens is a price book SKU mismatch,
which `pricing sync --deployment` fixes (ADR-0018).

### Streaming

`stream: true` returns server-sent events in the OpenAI chunk format, so `client.chat.completions.create(..., stream=True)`
works unchanged. Policy checks and the budget reservation run before the first byte. The cost
is known only when the provider sends its final usage, so it travels in the last event instead
of a header: a chunk with empty `choices`, the `usage` object and the full `x_finops` block,
then `data: [DONE]`. `X-FinOps-Request-Id` is set as a header. If the provider ends the stream
without usage, the call is billed at the reservation estimate and the final event carries
`usage_estimated: true`; if the upstream fails mid-stream, the reservation is released, nothing
is recorded and the client receives one `data: {"error": ...}` event (ADR-0012).

Other rejections: `401 missing_api_key / invalid_api_key / api_key_expired / api_key_disabled`,
`403 team_mismatch / project_not_allowed / model_not_allowed`, `429 rate_limited` with
`Retry-After`, `422` unpriced model, `400` missing project, `429 upstream_rate_limited` when
the provider throttles (its `Retry-After` is passed through), `502` upstream error (in both
upstream cases nothing is recorded and the reservation is released).

### Point an OpenAI SDK at it

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="lfg_...",  # the team key
    default_headers={"X-Project": "demo", "X-Use-Case": "chat"},
)
client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
```

## Policy as code

[`config/policies.yaml`](config/policies.yaml):

```yaml
version: "2026-11-01"
defaults:
  allowed_models: ["gpt-4o-mini", "llama3.2:3b"]
  budget: { monthly_usd: 50, warn_at: 0.8, block_at: 1.0, enforcement: block }
teams:
  platform:
    allowed_models: ["*"]
    budget: { monthly_usd: 500 }
  research:
    budget: { monthly_usd: 20, warn_at: 0.5, enforcement: warn }   # never blocks, only warns
```

- `warn_at` and `block_at` are fractions of `monthly_usd`; the budget is month to date (UTC).
- Before each call the gateway reserves an estimate (prompt chars / 4, output = `max_tokens` or
  `reserve_output_tokens`, default 1024) with one atomic conditional update; after the call it
  settles the actual cost; on upstream failure it releases the reservation (ADR-0007).
- `enforcement: warn` keeps the warnings and the log line but never returns 403.
- `rate_limit.requests_per_minute` rejects bursts with 429 and `Retry-After`; per gateway
  instance (ADR-0008).
- A team not listed gets `defaults`; a listed team overrides only the keys it sets.
- Unknown keys fail at startup. No policy file means no budgets and a startup warning.

## Endpoints

| Method | Path                     | Purpose                                                    |
| ------ | ------------------------ | ---------------------------------------------------------- |
| POST   | `/v1/chat/completions`   | OpenAI-compatible chat completion, `stream: true` supported |
| GET    | `/v1/models`             | Models the price book knows, and which provider serves each |
| GET    | `/finops`                | Report page: budgets and chargeback, HTML, no build step   |
| GET    | `/v1/finops/summary`     | Cost grouped by team / project / model, filterable         |
| GET    | `/v1/finops/summary.csv` | The same rows as CSV for spreadsheets and BI tools         |
| GET    | `/v1/finops/budgets`     | Month-to-date budget, rate limit and allow-list per team   |
| GET    | `/metrics`               | Prometheus: requests by outcome, cost, tokens, latency, budget ratio |
| GET    | `/healthz`               | Liveness, version, auth mode, price book and policy versions |
| GET    | `/docs`                  | OpenAPI UI                                                 |

![The /finops report page](docs/images/finops-report.png)

Reports follow one access rule (ADR-0016): with `LFG_AUTH_MODE=api_key` a team key reads its
own team, a key with `role: finops` (`keys new --role finops`) reads every team, and the HTML
page asks for the key once and keeps it in a session cookie. The page sorts on any column,
filters rows, selects teams (all or a subset), switches the display currency
(`LFG_REPORT_RATES`, display only; the ledger stays in the book's currency), and opens the
effective policy per team and the price book from their version links. How spreadsheets and BI
tools read the same data is in [docs/reporting.md](docs/reporting.md).

## Configuration

Environment variables (prefix `LFG_`; a `.env` file is read if present):

| Variable                        | Default                   | Notes                                   |
| ------------------------------- | ------------------------- | --------------------------------------- |
| `LFG_PROVIDER`                  | `ollama`                  | default provider: `ollama`, `azure_openai` or `openai_compat`; all configured ones are available |
| `LFG_OLLAMA_BASE_URL`           | `http://localhost:11434`  |                                         |
| `LFG_AZURE_OPENAI_ENDPOINT`     |                           | required for `azure_openai`             |
| `LFG_AZURE_OPENAI_API_KEY`      |                           | required for `azure_openai` with `api_key` auth |
| `LFG_AZURE_OPENAI_AUTH`         | `api_key`                 | `api_key` or `entra` (managed identity / `az login`, needs `[azure]`) |
| `LFG_AZURE_OPENAI_API_VERSION`  | `2024-10-21`              |                                         |
| `LFG_AZURE_OPENAI_DEPLOYMENTS`  | `{}`                      | JSON map model name -> deployment       |
| `LFG_OPENAI_COMPAT_BASE_URL`    |                           | required for `openai_compat`; includes the API prefix (`.../openai/v1`, `.../v1`) |
| `LFG_OPENAI_COMPAT_API_KEY`     |                           | sent as `Authorization: Bearer`; optional |
| `LFG_OPENAI_COMPAT_HEADERS`     | `{}`                      | JSON map of extra request headers       |
| `LFG_OPENAI_COMPAT_MODELS`      | `{}`                      | JSON map model name -> upstream model or deployment name |
| `LFG_AUTH_MODE`                 | `api_key`                 | `api_key` or `headers` (dev only)       |
| `LFG_KEYS_PATH`                 | `config/keys.yaml`        | hashes only                             |
| `LFG_PRICING_PATH`              | `config/pricing.yaml`     |                                         |
| `LFG_POLICIES_PATH`             | `config/policies.yaml`    | missing file = no policies              |
| `LFG_DATABASE_URL`              | `sqlite:///./gateway.db`  | any SQLAlchemy URL; PostgreSQL via `pip install ".[postgres]"` and `postgresql+psycopg://...` |
| `LFG_REQUEST_TIMEOUT_SECONDS`   | `120`                     |                                         |
| `LFG_REPORT_RATES`              | `{}`                      | display currencies on `/finops` as units per 1 book unit, e.g. `{"EUR": 0.88}` |
| `LFG_ALERT_WEBHOOK_URL`         |                           | Slack/Teams incoming webhook or any JSON endpoint; empty = off |
| `LFG_ALERT_COOLDOWN_SECONDS`    | `3600`                    | same event per team/provider at most this often |
| `LFG_ALERT_FORMAT`              | `slack`                   | `slack` (text + JSON fields) or `teams` (Adaptive Card) |
| `LFG_METRICS_ENABLED`           | `true`                    | serve `/metrics`                        |
| `LFG_OTEL_ENABLED`              | `false`                   | needs `pip install ".[otel]"`; exporter via `OTEL_EXPORTER_OTLP_*` |
| `LFG_OTEL_SERVICE_NAME`         | `llm-finops-gateway`      |                                         |

### Tracing to Langfuse, Grafana or Azure Monitor

```bash
pip install "llm-finops-gateway[otel]"
export LFG_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel   # or your collector
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic%20$(printf 'pk-lf-...:sk-lf-...' | base64)"
```

Every request span carries `finops.team`, `finops.project`, `finops.use_case`, `finops.model`,
`finops.key_id`, `finops.cost_usd`, `finops.input_tokens`, `finops.output_tokens`,
`finops.request_id` and, when rejected, `finops.policy_violation`.

## Alerts to Slack, Teams or any webhook

```dotenv
LFG_ALERT_WEBHOOK_URL=https://hooks.slack.com/services/T000/B000/xxxx
LFG_ALERT_COOLDOWN_SECONDS=3600
```

The gateway posts one event when a team crosses its warning threshold (`budget_warn`), when a
call is refused for budget (`budget_block`), and when the provider fails (`upstream_error`,
`upstream_rate_limited`). `LFG_ALERT_FORMAT=slack` (default) sends `text` plus flat JSON
fields, which a Slack incoming webhook renders and any JSON receiver can parse;
`LFG_ALERT_FORMAT=teams` sends an Adaptive Card for a Teams Workflows webhook.
`llm-finops-gateway alerts test` sends one sample event so you can check the channel. The same event for the same
team or provider is sent once per cooldown. Delivery runs in the background and never delays
or fails a request (ADR-0017). Thresholds and routing for a monitoring stack belong to the
Prometheus metrics below.

## Grafana dashboard and SDK examples

[`grafana/llm-finops-gateway.json`](grafana/llm-finops-gateway.json) is a ready-made dashboard
for the `/metrics` endpoint: spend, calls served and rejected, budget used per team with the
warn and block thresholds, spend rate by team, requests by outcome, tokens, upstream latency
p50/p95 by provider, and a spend table by team, project and model. Import it into Grafana or
Azure Managed Grafana and pick the Prometheus data source that scrapes the gateway, or run it
locally against a gateway on port 8080:

```bash
docker compose -f docker-compose.observability.yml up -d   # Prometheus + Grafana, dashboard provisioned
open http://localhost:3000                                 # viewer without login; admin / admin to edit
```

![The Grafana dashboard](docs/images/grafana-dashboard.png)

Where each view lives in Azure: the report page is served by the gateway itself, so it is the
Container App's URL plus `/finops`, behind the gateway's key auth and whatever sits in front
(Container Apps authentication with Entra ID, or APIM); finance pulls CSV or JSON into whatever
BI tool it already has, or reads the ledger database with the read-only role; operations scrape `/metrics` with Azure Monitor managed
Prometheus and import this dashboard into Azure Managed Grafana; alerts go to Slack or Teams
through the webhook, or come from Azure Monitor alert rules on the same metrics. Azure Cost
Management only ever sees the Azure OpenAI resource's bill; the team dimension exists because
of the gateway. Details in [docs/reporting.md](docs/reporting.md).

[`examples/`](examples/) has the same four calls in Python and TypeScript with the official
OpenAI SDKs: a completion with the `x_finops` block, a stream with the cost in the final chunk,
a tool-call round trip, and a rejected call.

## Deploy to Azure

[`deploy/azure/`](deploy/azure/) has one Bicep file and a short README: a Container App with a
user-assigned managed identity that calls Azure OpenAI without a key (ADR-0014), an Azure
Database for PostgreSQL for the ledger (SQLite on Azure Files as the cheap fallback), Log
Analytics, a Key Vault for the secrets, and the image built in the cloud with `az acr build`.
About 1.2 USD a day for an always-on demo; what to add for production is listed there (Entra
ID in front of `/finops`, private networking). The ledger schema and the database roles are
in [docs/database.md](docs/database.md).

## Docker

```bash
docker compose up --build          # gateway + Ollama; needs Docker Compose 2.24 or newer
docker build -t llm-finops-gateway . && docker run --env-file .env -p 8080:8080 llm-finops-gateway
```

## Development

```bash
pre-commit install
ruff check . && ruff format .
mypy src tests
pytest -q
```

CI runs the same steps on 3.11 and 3.12, plus `pip-audit` and a Docker build.
Architecture notes: [docs/architecture.md](docs/architecture.md). Operations:
[docs/deployment.md](docs/deployment.md). Manual test plan with the latest results:
[docs/testing-plan.md](docs/testing-plan.md).

## Roadmap

- **Next**: OIDC attribution (Entra ID app roles, Keycloak client roles) alongside team keys;
  evaluation replay and regression scoring ([ADR-0009](docs/adr/0009-evaluation-replay.md),
  design proposed).
- **Then**: token budgets and amortised or shared-pool pricing for self-hosted models;
  FOCUS-format export of the ledger; schema migrations (Alembic) instead of `create_all`.
- **Later**: PII and content hooks; shared rate limits (Redis) for multi-replica deployments;
  Helm chart; Anthropic Messages adapter and Claude in Microsoft Foundry.

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md), the
[Code of Conduct](CODE_OF_CONDUCT.md) and the [CLA](CLA.md) first. Project decision-making is
described in [GOVERNANCE.md](GOVERNANCE.md).

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
