# Deployment guide: putting the gateway into daily operation

## How it fits

Applications stop calling the model provider directly and call the gateway instead. The
gateway holds the provider credentials; the applications hold a team key.

```
                    team key (lfg_...)                      provider key (Key Vault)
  app A (team: support) ───┐                                        │
  app B (team: platform) ──┼──▶  llm-finops-gateway  ──────────────▶ Azure OpenAI (private endpoint)
  notebook (team: research)┘       │        │                        or Ollama
                                   │        └──▶ PostgreSQL (ledger + budget counters)
                                   └──▶ Prometheus /metrics, OTLP traces (Grafana, Langfuse, Azure Monitor)
```

Only the gateway can reach the provider. If a team wants a new model, it goes into
`pricing.yaml` and the team's allow-list, in a pull request.

## What changes in an application

Two values. With the OpenAI Python SDK:

```python
client = OpenAI(
    base_url="https://llm-gateway.internal/v1",
    api_key=os.environ["LLM_GATEWAY_KEY"],  # team key from the platform team
    default_headers={"X-Project": "ticket-triage", "X-Use-Case": "summarise"},
)
```

LangChain, LlamaIndex, Semantic Kernel, curl: same two values (`base_url` / `api_key`, plus
the project header). Streaming is not supported yet, so set `stream=False` where a library
defaults to streaming.

## Issuing and rotating team keys

```bash
llm-finops-gateway keys new --team customer-support --id support-triage --project ticket-triage
```

Prints the raw key once and a YAML entry. Put the entry in `config/keys.yaml` (hash only),
deploy, hand the raw key to the team through your secret store (Key Vault, 1Password, the
team's CI secrets). Never paste raw keys into tickets or chat.

Rotation: add the new entry, deploy, switch the application, set `disabled: true` on the old
entry, deploy. Expiry dates (`expires:`) make rotation a calendar item instead of a surprise.

One key per application or per CI pipeline is the sweet spot: fine enough to trace a leak,
coarse enough to manage.

## Rollout plan that does not break anyone

| Week | Step | Policy |
| --- | --- | --- |
| 1 | Deploy the gateway next to the existing provider endpoint; issue keys; migrate one friendly team | all teams `enforcement: warn`, generous budgets |
| 2 | Migrate the rest; publish the first `/v1/finops/summary` report; agree monthly budgets with each team lead | still warn-only |
| 3 | Switch production teams to `enforcement: block`; keep research/experimentation on warn | real budgets, allow-lists reviewed |
| 4 | Block direct access to the provider (network policy or key rotation) so the gateway is the only path | steady state |

Every step is a config change in git with a reviewer, which is also your audit trail.

## Reference deployment on Azure

- **Compute:** Azure Container Apps, 1 to 3 replicas, min 1. The image is non-root and has a
  health check. Set `LFG_DATABASE_URL` to PostgreSQL; SQLite is for one replica only.
- **Secrets:** none for the provider when the Container App's managed identity has the
  `Cognitive Services OpenAI User` role on the Azure OpenAI resource and the gateway runs with
  `LFG_AZURE_OPENAI_AUTH=entra` (ADR-0014). With `api_key` auth, `LFG_AZURE_OPENAI_API_KEY` as a
  Container Apps secret referenced from Key Vault.
- **Network:** private endpoint to Azure OpenAI, internal ingress for the gateway, apps on the
  same VNet or via Private Link. Expose `/metrics` only internally.
- **Config:** `config/` mounted from the container image (rebuild on change) or from a volume
  pointing at a git-synced share. Price book, policies and key hashes are all in that folder.
- **Observability:** scrape `/metrics` with Azure Monitor managed Prometheus or Grafana;
  `LFG_OTEL_ENABLED=true` with the OTLP exporter pointed at the Azure Monitor OpenTelemetry
  collector, Grafana Tempo, or Langfuse.
- **Rate limits** are per replica (ADR-0008); with 3 replicas the effective limit is 3x.

A Terraform module for this shape is on the roadmap; until then the `az` commands in
[`azure-setup.md`](azure-setup.md) cover the OpenAI side and the Container App is a standard
deployment.

## Day-two operations

| Task | How |
| --- | --- |
| Monthly chargeback | `GET /v1/finops/summary` (or a SQL query on `cost_records`), reconcile with the cloud bill; the `price_book_version` column explains any delta |
| Budget review | `GET /v1/finops/budgets`; the Prometheus gauge `lfg_budget_used_ratio{team}` for alerts at 0.8 and 1.0 |
| Price change | edit `pricing.yaml`, bump `version`, PR, deploy. Old rows keep their version |
| New model | add it to `pricing.yaml` **and** to the allow-lists that may use it |
| Leaked key | set `disabled: true`, deploy, issue a new one; the ledger's `key_id` shows what it was used for |
| Counter drift | rebuild `budget_counters` from `cost_records` for the month (a `ledger reconcile` CLI command is planned) |

## Alerts worth having from day one

- `lfg_budget_used_ratio > 0.8` for any team: tell the team lead before the block happens.
- `rate(lfg_requests_total{outcome="upstream_error"}[5m]) > 0`: the provider or the key is
  the problem, not the teams.
- `rate(lfg_requests_total{outcome="unpriced"}[1h]) > 0`: somebody is trying a model that is
  not in the price book; usually a sign that a team wants something and should ask.
