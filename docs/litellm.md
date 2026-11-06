# Running the FinOps rules inside LiteLLM

The gateway's rules can run inside an existing LiteLLM proxy as a callback plugin (ADR-0019):
no second proxy, the same price book, policy file and ledger. This page is the plugin's guide;
the last section records what the LiteLLM hooks can see, as measured on 2026-11-05 with
LiteLLM 1.108.0 (`ghcr.io/berriai/litellm:main-stable`, digest
`sha256:c560d317136880ea92d62334d4367ab10955b1af189dd3ea24f6551290b1d6c1`), and
will become the plugin's user guide.

## Install

```bash
pip install "llm-finops-gateway[litellm]"      # pins LiteLLM to the tested minor version
```

In the proxy config, register the plugin instance (an instance, not the class):

```yaml
litellm_settings:
  callbacks: llm_finops_gateway.litellm_plugin.plugin
```

The plugin reads the gateway's settings from the environment: `LFG_PRICING_PATH`,
`LFG_POLICIES_PATH` and `LFG_DATABASE_URL`. Point them at the same files and database the
gateway uses, or at a copy; `examples/litellm/` has a complete compose stack, a Dockerfile
that installs the package into the official LiteLLM image, and `demo.sh`.

## What it does per chat call

| Step | LiteLLM hook | Behaviour |
| --- | --- | --- |
| Attribution | `async_pre_call_hook` | Team = the `team_alias` of the LiteLLM team the key belongs to; project from `X-Project` (required), use case from `X-Use-Case`. A key without a team, or a call without a project: `400 missing_attribution`, before the call |
| Price | same hook | Model not in the price book: `422`, before the call. LiteLLM's own cost map is not consulted |
| Policy | same hook | Project registry (`403 project_not_registered`, `403 use_case_not_registered`), allow-list, rate limit (`429` with `Retry-After`) and the atomic team and project budget reservations (`403 budget_exceeded`, or a warning at the warn threshold), exactly as in the gateway |
| Settle | `async_post_call_response_headers_hook` (non-streaming) or `async_log_success_event` (streaming) | Usage from the response, cost from the price book, one ledger row and the reservation settled in one transaction |
| Release | `async_log_failure_event` | The reservation is given back when the upstream call fails |

Headers on every settled non-streaming response: `X-FinOps-Request-Id` (prefix `lt-`),
`X-FinOps-Cost-USD`, `X-FinOps-Currency`, `X-FinOps-Cached-Input-Tokens`, `X-FinOps-Budget-*`,
`X-FinOps-Warning`. Streamed
responses carry the request id and the budget state as of the start of the call; their cost
is settled when the stream ends and is visible in the ledger and the report page. The response
body is never modified: there is no `x_finops` block in LiteLLM mode.

The ledger row holds metadata only: team, project, use case, key alias, provider, model,
token counts, cost and the price book version. Prompts, completions and tool payloads are never
stored by the plugin; LiteLLM's own request log is a separate, configurable feature.

The price book does not update itself: `llm-finops-gateway pricing sync` compares the file
with the Azure Retail Prices API and prints the differences, `--write` rewrites the rows and
bumps the version, and the change goes through a pull request like any other policy change.
Run it on whatever cadence the organisation reviews prices (weekly, or when a model is added).
The plugin watches both files and picks an edit up on the next call, no restart (ADR-0022);
mount the directory, not the single file, so that a rename-based edit is visible.
The ledger grows by one row per served call and one counter per team, project and month, and
is never pruned by the plugin.

The team name in `policies.yaml` must equal the LiteLLM `team_alias`. LiteLLM's own key and
team budgets keep working next to the plugin; pick one of the two as the number teams are
told about.

## Try it locally

`examples/litellm/docker-compose.yml` starts four services next to a PostgreSQL store for
LiteLLM: the proxy with a probe callback only (port 4000, the "before" picture), the proxy with
the plugin (port 4001), the gateway's report page over the plugin's ledger (port 8081) and the
Streamlit assistant (port 8501, `examples/assistant/`: roles, receipt, docs Q&A).

```bash
cd examples/litellm
docker compose --env-file ../../.env up -d --build      # Azure OpenAI endpoint and key from .env
./demo.sh                                               # creates two teams and keys, runs six calls
open http://localhost:8081/finops                       # the same report page the gateway serves
open http://localhost:8501                              # the assistant: pick a role, chat, read the receipt
open http://localhost:4001/ui                           # LiteLLM admin UI, admin / the master key
```

`demo.sh` keeps the virtual keys in `.demo-keys/` (git-ignored) and writes them for the
assistant container, which reads them on its next start. The example policies also define
three role teams for the assistant: `ceo` (any model, a budget it will not reach),
`team-leads` (two models, a small budget) and `employees` (the cheapest model, the smallest
budget). The calls by hand, with the customer-support key:

```bash
KEY=$(cat .demo-keys/customer-support.key)
curl -s -D - -o /dev/null http://localhost:4001/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H "X-Project: chat-assistant" -H "X-Use-Case: faq" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4.1-mini","messages":[{"role":"user","content":"Say OK"}],"max_tokens":5}' \
  | grep -i x-finops
curl -s http://localhost:8081/v1/finops/summary | python3 -m json.tool
```

The plugin publishes the gateway's own `lfg_*` metrics (requests by outcome, cost, tokens,
upstream latency, budget ratio) into the registry LiteLLM serves on `/metrics/` (trailing
slash; the master key as bearer token), next to LiteLLM's `litellm_*` series. The shipped
Grafana dashboard therefore shows LiteLLM traffic unchanged. The local Prometheus of
`docker-compose.observability.yml` scrapes it with this job (already in
`observability/prometheus.yml`):

```yaml
  - job_name: litellm
    metrics_path: /metrics/
    authorization: { credentials: sk-lfg-master-local-only }
    static_configs:
      - targets: ["host.docker.internal:4001"]
```

Enable LiteLLM's own `prometheus` callback in `litellm_settings.callbacks` next to the plugin
(as `config.plugin.yaml` does) so that the endpoint exists; the plugin's series are served
there regardless of which callbacks are on.

## The report page inside the LiteLLM dashboard

The gateway's report page can be a LiteLLM dashboard plugin (ADR-0024): the dashboard shows
"FinOps" in its mode switcher, loads the report in a frame and signs the viewer in with the
identity it holds, so admins get one console. Two settings on the report service and one
entry in the proxy config:

```yaml
# LiteLLM config.yaml
general_settings:
  plugins:
    - name: finops
      display_name: FinOps
      url: https://finops.example.com      # the URL browsers reach the report at
```

```bash
# the report service (the gateway, pip install "llm-finops-gateway[litellm-ui]")
LFG_LITELLM_PLUGIN_NAME=finops
LFG_LITELLM_SALT_KEY=<the proxy's LITELLM_SALT_KEY>
```

LiteLLM derives a key per plugin from its salt key and encrypts a 30-second identity claim
with it; the report service verifies the claim, admits `proxy_admin` and `proxy_admin_viewer`
only, and sets an HttpOnly session cookie (`SameSite=None; Secure`, eight hours) that gives
the report the scope of a `role: finops` key. Because the report is a third-party frame, the
cookie needs HTTPS or `localhost`; the example stack serves it on `http://localhost:8081`.
Nothing changes for team keys and finops keys outside the dashboard.

## What it does not do (yet)

- No webhook alerts from the plugin yet (LiteLLM has its own alerting); metrics are covered
  above.
- Only chat completions are priced; embeddings and other call types pass through untouched.
- Per-user attribution: the plugin keys calls by team; a user id from LiteLLM is not
  recorded on the ledger row.

## Latency

Measured on 2026-11-05 against Azure OpenAI from a laptop, 40 calls each after warm-up,
through the same LiteLLM image with and without the plugin: median 816 ms versus 759 ms,
p95 1296 ms versus 905 ms. The difference is within the run-to-run noise of the upstream call;
the plugin's own work is in-memory checks plus one SQLite or PostgreSQL transaction.

## What the hooks see (spike, 2026-11-05)

Measured with `examples/litellm/` (a LiteLLM proxy, a PostgreSQL store and a probe callback
that prints every hook call), a team `customer-support` created with `/team/new` and a key
created with `/key/generate` under it.

| Fact | Result |
| --- | --- |
| Request headers in `async_pre_call_hook` | Yes: `data["proxy_server_request"]["headers"]`, lower-cased keys (`x-project`, `x-use-case`) |
| Team of the calling key | Yes: `user_api_key_dict.team_alias` (`customer-support`), `team_id`, `key_alias` |
| Metadata set in the pre-call hook | Reaches the headers hook (`data["metadata"]`) and the success and failure events (`kwargs["litellm_params"]["metadata"]`). LiteLLM fills `metadata` with many keys of its own, so the plugin uses one namespaced key |
| Headers hook, non-streaming | Runs after the upstream call with the `ModelResponse` (usage available); headers it returns reach the client |
| Headers hook, streaming | Runs before the stream completes (a stream wrapper, no usage yet): only the request id can be set, cost is settled from the success event |
| Success event, streaming | Fires once the stream ends, with the final usage (`prompt_tokens`, `completion_tokens`, `cached_tokens`) and `response_cost` |
| Model names in the success event | `kwargs["model"]` is the deployment (`gpt-4.1-mini-dev`), `standard_logging_object["model"]` is `azure/gpt-4.1-mini-dev`; the name the client used comes from the pre-call metadata |
| Unpriced model (`input_cost_per_token: 0`) | 200 OK, `x-litellm-response-cost: 0`: nothing stops it |
| Call without `X-Project` | 200 OK: LiteLLM has no notion of a project |

Two configuration notes from the same run:

- With `STORE_MODEL_IN_DB=True` and a PostgreSQL store, the `model_list` of `config.yaml`
  was not served on this version (only logged); without it all three models load. The
  example compose file does not set it.
- `callbacks:` must point at an instance (`spike_probe.probe`), not the class.
