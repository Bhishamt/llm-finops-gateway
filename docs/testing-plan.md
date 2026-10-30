# Manual test plan

Scope: the gateway on a laptop (Ollama, or the simulated demo if Ollama is unavailable) and on
a disposable Azure OpenAI resource. Automated tests (`pytest -q`, 111 cases) cover the same
behaviour with a fake provider; this plan is for seeing it end to end and for collecting the
screenshots used in the README and the launch posts.

Last run: 2026-10-28 on macOS (Apple M1, 8 GB), Python 3.12, Ollama 0.40 with `llama3.2:3b`.
Sections 0 to 3c passed; see the sign-off table and the observations at the end.

Each step lists the command, the expected result and what to capture. Tick as you go.

## 0. Preconditions (10 min)

```bash
cd llm-finops-gateway
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,demo,otel]"
ruff check . && ruff format --check . && mypy src tests && pytest -q
```

- [ ] All four green. Capture: terminal with `111 passed`.

## 1. Simulated demo, no keys (5 min)

```bash
python demo/run_demo.py
open demo/out/chargeback.png      # macOS; xdg-open on Linux
cat demo/out/report.md
```

- [ ] Table shows five teams; `customer-support` at `100.x% (block)`, `platform` in the high
      90s `(warn)` with 2 budget blocks (reservations that did not fit), `research` at
      `>200% (warn)` warn-only, `marketing` with 9 `rate limited` and 5 `unpriced model`,
      `shadow-it` with 14 `no owner` rejections. "Unallocated spend: 0.00 USD".
- [ ] `demo/out/chargeback.png` renders two panels, labels readable.
- [ ] Capture: the PNG and the Markdown table (both go in the launch posts).

## 2. Local gateway against Ollama (20 min)

Terminal A:

```bash
ollama pull llama3.2:3b && ollama serve
```

Terminal B (headers mode, no keys yet):

```bash
LFG_AUTH_MODE=headers llm-finops-gateway serve --port 8080
```

Terminal C, run in order:

| # | Command | Expected |
| - | ------- | -------- |
| 2.1 | `curl -s localhost:8080/healthz` | `status: ok`, `price_book` and `policies` versions shown |
| 2.2 | `curl -s localhost:8080/v1/models \| jq '.data[].id'` | the models listed in `config/pricing.yaml` |
| 2.3 | call **without** `X-Team` | HTTP 400, `missing_headers: ["X-Team"]`, hint text |
| 2.4 | call with `X-Team: a b` (space) | HTTP 400, regex message |
| 2.5 | call with `"model": "gpt-9"` | HTTP 422, `No price configured for model 'gpt-9'` |
| 2.6 | call as `X-Team: customer-support` with `"model": "llama3.2:3b"` | HTTP 403, `model_not_allowed`, message lists allowed models |
| 2.7 | call as `X-Team: platform` with `llama3.2:3b` | HTTP 200, `x_finops.cost_usd` = `0` (local model), headers `X-FinOps-Budget-Status: ok` |
| 2.8 | `curl -s localhost:8080/v1/finops/summary \| jq` | one row, `calls: 1` |
| 2.9 | `curl -s localhost:8080/v1/finops/budgets \| jq` | every team from `policies.yaml`, `platform` at `0.0%` |
| 2.10 | `curl -s localhost:8080/docs` in a browser | OpenAPI UI lists 5 routes |
| 2.11 | `curl -s localhost:8080/metrics \| grep lfg_` | `lfg_requests_total`, `lfg_cost_usd_total`, `lfg_budget_used_ratio` lines |

Call template:

```bash
curl -s -i localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Team: platform" -H "X-Project: smoke" -H "X-Use-Case: manual-test" \
  -d '{"model":"llama3.2:3b","messages":[{"role":"user","content":"One sentence on FinOps."}]}'
```

- [ ] 2.1 to 2.10 pass. Capture: 2.3, 2.6 and 2.7 responses (headers visible with `-i`).

## 3. Budget lifecycle with a tiny budget (15 min)

Stop the gateway. Point it at the demo policies, whose budgets are cents:

```bash
LFG_POLICIES_PATH=demo/policies.yaml LFG_DATABASE_URL=sqlite:///./lifecycle.db \
  llm-finops-gateway --port 8080
```

Local models cost 0, so seed spend with the ledger directly (one-off, in a second shell with the
venv active). `Ledger.record()` updates both the ledger and the budget counter:

```bash
python - <<'PY'
from decimal import Decimal
from llm_finops_gateway.ledger import CostEntry, Ledger
Ledger("sqlite:///./lifecycle.db").record(CostEntry(
    request_id="seed-1", team="platform", project="seed", use_case="seed",
    provider="manual", model="gpt-4o-mini", input_tokens=0, output_tokens=0,
    cost_usd=Decimal("0.13"), price_book_version="manual"))
PY
```

`platform` in the demo policy has a 0.15 USD budget, warn at 80 %: 0.13 is 86.7 %.

| # | Command | Expected |
| - | ------- | -------- |
| 3.1 | call as `platform` | HTTP 200, `X-FinOps-Budget-Status: warn`, `X-FinOps-Warning` sentence with "blocked at 100%", `x_finops.policy.warnings[0]` same sentence, gateway log line `WARNING ... policy warn team=platform` |
| 3.2 | seed another `0.02` (total 0.15 = 100 %), call again | HTTP 403, `error: budget_exceeded`, `budget.level: block`, `remaining_usd: 0`, message ends with "estimated at 0 USD" (free model, still blocked), log line `policy block ... reason=budget_exceeded` |
| 3.3 | call as `research` after seeding it `0.50` (500 % of 0.10) | HTTP 200, status `warn`, warning ends with "enforcement is warn-only" |
| 3.4 | `GET /v1/finops/budgets` | `platform` level `block`, `research` level `warn` with `enforcement: warn` |
| 3.5 | edit `demo/policies.yaml`, set `platform.budget.monthly_usd: 1.00`, restart, call | HTTP 200, status `ok` (policy change is a file change, no DB edit) |

Note for 3.3: `research` needs its own key in `api_key` mode (`keys new --team research ...`)
and `llama3.2:3b` added to its allow-list in `demo/policies.yaml`; with the platform key plus
`X-Team: research` you get 403 `team_mismatch` instead, which is also correct.

- [ ] 3.1 to 3.5 pass. Capture: 3.1 response headers + log line side by side, 3.2 JSON body.
- [ ] `rm lifecycle.db` afterwards.

## 3b. Team keys (15 min)

```bash
llm-finops-gateway keys new --team platform --id platform-laptop
# paste the printed entry under keys: in config/keys.yaml, keep the raw key in a shell variable
export KEY=lfg_...
llm-finops-gateway keys check
llm-finops-gateway serve --port 8080        # api_key mode is the default
```

| # | Command | Expected |
| - | ------- | -------- |
| 3b.1 | call with `X-Team` and no `Authorization` | HTTP 401 `missing_api_key`, `WWW-Authenticate: Bearer` |
| 3b.2 | `-H "Authorization: Bearer lfg_wrong"` | HTTP 401 `invalid_api_key` |
| 3b.3 | `-H "Authorization: Bearer $KEY" -H "X-Project: smoke"` | HTTP 200, `x_finops.team` = `platform`, `x_finops.key_id` = `platform-laptop` |
| 3b.4 | same plus `-H "X-Team: research"` | HTTP 403 `team_mismatch` |
| 3b.5 | set `disabled: true` on the entry, restart, call | HTTP 401 `api_key_disabled` |
| 3b.6 | `python demo/run_demo.py --mode live --url http://localhost:8080 --model llama3.2:3b --api-key $KEY --calls 20` | all calls attributed to `platform` regardless of the simulated teams |

- [ ] 3b.1 to 3b.6 pass. Capture: 3b.3 body showing `key_id`, 3b.4 body.

## 3c. Rate limit (5 min)

Set `rate_limit: { requests_per_minute: 3 }` on `platform` in the policy file you are running
with, restart.

```bash
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "%{http_code} " $U -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $KEY" -H 'X-Project: smoke' -d "$B"
done; echo
```

| # | Command | Expected |
| - | ------- | -------- |
| 3c.1 | four quick calls in a loop | three served (or `403` if the budget from section 3 is still exhausted), the fourth `429` with `Retry-After` and `error: rate_limited`. The rate limit runs before the budget check, so `403 403 403 429` is the expected pattern right after 3.2 |
| 3c.2 | `GET /v1/finops/budgets` | `platform.requests_per_minute: 3` |

- [ ] Capture: the 429 with headers.

## 3d. Tracing (10 min, optional)

```bash
docker run --rm -p 4318:4318 -p 16686:16686 jaegertracing/jaeger:2.5.0
LFG_OTEL_ENABLED=true OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  LFG_AUTH_MODE=headers llm-finops-gateway serve --port 8080
```

- [ ] After a few calls, Jaeger at `localhost:16686` shows `POST /v1/chat/completions` spans
      with `finops.team`, `finops.cost_usd` attributes; `/healthz` is not traced.
- [ ] Capture: one span's attribute panel. For Langfuse, point the endpoint at
      `https://cloud.langfuse.com/api/public/otel` with the Basic header (see README).

## 4. Azure OpenAI, real money (30 min, well under 1 USD)

Follow `docs/azure-setup.md` to create the resource and `.env`. Then:

```bash
llm-finops-gateway --port 8080
python demo/run_demo.py --mode live --url http://localhost:8080 --model gpt-4.1-mini --calls 30
```

| # | Check | Expected |
| - | ----- | -------- |
| 4.1 | Live report | ~30 calls, non-zero `spend USD`, teams priced with `gpt-4.1-mini` (`gpt-4o-mini` can no longer be deployed, see `docs/azure-setup.md`) |
| 4.2 | One `x_finops.cost_usd` by hand | `(prompt_tokens * 0.40 + completion_tokens * 1.60) / 1_000_000` matches to the last digit |
| 4.3 | `GET /v1/finops/summary` | sums equal the report |
| 4.4 | Wrong deployment mapping (`LFG_AZURE_OPENAI_DEPLOYMENTS={"gpt-4.1-mini":"nope"}`) | HTTP 502, `upstream_error`, provider `azure_openai`, no ledger row written |
| 4.6 | Same resource through the v1 endpoint: `LFG_PROVIDER=openai_compat`, base URL `.../openai/v1` | HTTP 200, `x_finops.provider: openai_compat`, cost matches 4.2 |
| 4.8 | `stream: true` on `gpt-4.1-mini` and `gpt-5-mini` | `text/event-stream`, `X-FinOps-Request-Id` header, `chat.completion.chunk` deltas, final event with `usage` from the provider (`usage_estimated` absent) and the `x_finops` block, `data: [DONE]`; one ledger row per stream |
| 4.9 | Keyless: `LFG_AZURE_OPENAI_AUTH=entra`, no `LFG_AZURE_OPENAI_API_KEY`, after `az role assignment create ... --role "Cognitive Services OpenAI User"` | HTTP 200 with `Authorization: Bearer` from the `az login` identity; before the role propagates Azure answers `PermissionDenied`, returned as 502 |
| 4.10 | Tool calling on `gpt-4.1-mini`: a `get_weather` tool, two rounds (tool call, then tool result), the same streamed, `response_format: json_object`, content parts, and a request with an unknown field | round 1 `finish_reason: tool_calls` with arguments; round 2 a sentence using the tool result; streamed `delta.tool_calls` fragments assemble to the same call; JSON mode returns JSON; content parts accepted; unknown field is 422 naming it |
| 4.7 | Reasoning model (`gpt-5-mini` deployment, price book row `reasoning: true`) called with `temperature` and `max_tokens` the way OpenAI SDK clients do | HTTP 200 (Azure would reject raw `max_tokens` and a non-default temperature); `usage.completion_tokens_details.reasoning_tokens` present; a small `max_tokens` yields empty content with `finish_reason: length` |
| 4.5 | Azure Cost Management, next day | the resource's token charges equal the gateway total to the cent (pay-as-you-go) |

- [ ] 4.1 to 4.5 pass. Capture: 4.1 table with real dollars, 4.5 screenshot of Cost Management
      next to the gateway summary. This pair is the strongest image for the launch post.
- [ ] `az group delete` when done.

## 5. Docker (10 min)

```bash
docker compose up --build
curl -s localhost:8080/healthz
docker compose exec ollama ollama pull llama3.2:3b
# repeat 2.7 against the container
docker compose down
```

- [ ] Image builds, runs as non-root, healthcheck green (`docker ps` shows `healthy`).

## 6. Known limitations to confirm, not fix

- [ ] A call whose *estimate* does not fit the remaining budget is rejected even if its actual
      cost would have (ADR-0007); lower `reserve_output_tokens` or send `max_tokens` to tighten.
- [ ] Rate limits are per gateway instance (ADR-0008).
- [ ] `stream: true` streams; the cost arrives in the final event, not in headers (ADR-0012).
- [ ] Budgets reset at UTC month start, not local time.

## Sign-off

| Section | Result | Date | Notes |
| ------- | ------ | ---- | ----- |
| 0 Preconditions | pass | 2026-10-28 | ruff, mypy strict, pytest green on Python 3.12 |
| 1 Simulated demo | pass | 2026-10-28 | 300 calls: 240 served, 60 rejected, 0.00 unallocated; chart rendered |
| 2 Ollama | pass | 2026-10-28 | 400 missing attribution, 403 model_not_allowed, 200 with `X-FinOps-*` headers, summary row; first call 7.4 s (model load), then about 3 s |
| 3 Budget lifecycle | pass (3.1, 3.2) | 2026-10-28 | 86.7 % warn with header and log line; 100 % block with structured 403; 3.3 to 3.5 not run |
| 3b Team keys | pass | 2026-10-28 | 401 missing_api_key with `WWW-Authenticate`, 401 invalid_api_key, 200 with `key_id`, 403 team_mismatch |
| 3c Rate limit | pass | 2026-10-28 | `403 403 403 429`, `Retry-After: 60`, log `reason=rate_limited rpm=3` |
| 3d Tracing | pass | 2026-10-30 | Jaeger 2.5.0 via OTLP/HTTP on 4318; `POST /v1/chat/completions` spans carry `finops.team`, `finops.project`, `finops.use_case`, `finops.model`, `finops.cost_usd`, `finops.input_tokens`, `finops.output_tokens`, `finops.request_id`; `/healthz` not traced |
| 4 Azure OpenAI | pass (4.1 to 4.4, 4.6) | 2026-10-30 | `gpt-4.1-mini-dev` in Sweden Central; 30-call run then an 800-call run (713 sent, 662 served, 0.0576 USD) with a policy copy that allows the model for every team; ledger total for the day 674 rows, 12,214 input and 33,448 output tokens, 0.0584024 USD; cost by hand matches to the last digit; 502 `DeploymentNotFound` with 0 ledger rows; v1 endpoint via `openai_compat` ok. 4.7 reasoning params on `gpt-5-mini-dev` (Global Standard) pass 2026-10-30: 200 with translated params, 60-token limit gave empty content and `length`, 400-token limit gave "Ready" with 74 completion tokens, cost 0.00015125 USD matches by hand. 4.8 streaming pass 2026-10-30: 13 events, text intact, final usage 16 in / 10 out from Azure (not estimated), cost 0.0000224 USD, ledger row written; gpt-5-mini stream reported 300 reasoning tokens. 4.9 Entra: token flow verified 2026-10-30 (gateway sent a bearer token for the signed-in principal, Azure answered `PermissionDenied` because the role assignment was not yet made); the 200 is pending the role. 4.10 tool calling pass 2026-10-30: `tool_calls` round trip, 7 streamed fragments assembling `{"city":"Pecs"}`, JSON mode, content parts, 422 `extra_forbidden` for `foo`. 4.5 Cost Management pending (data lags 8 to 24 h) |
| 5 Docker | pass (image) | 2026-10-30 | image builds, runs as uid 10001, healthcheck `healthy`, live call through the container ok. `docker compose config` fails on Compose 2.23 (`env_file` long syntax needs 2.24+); compose with Ollama not brought up |
| 6 Limitations | confirmed by tests | 2026-10-28 | estimate-based rejection and per-instance rate limits covered by unit tests |

## Observations from the 2026-10-28 run (fixed in this release)

- Zero costs rendered as `0.0` in headers and body while the summary said `0`; all money
  strings now go through one formatter (`pricing.money`) and the suite has a regression test.
- `keys new` did not say that `keys: []` has to become `keys:` before pasting the entry; the
  CLI now prints that hint and the follow-up `keys check` command.
- Two checkouts of the repository with one venv caused a "file not found" moment; the venv and
  the config files must be in the same checkout, because `serve` reads `config/` relative to the
  working directory. Documented in the quick start.
- Ollama on macOS 13 is not installable from Homebrew (no bottles, source build fails) and the
  desktop app requires macOS 14; macOS 26 installed fine. The first `ollama pull` stalled on a
  flaky network with a DNS failure to the Cloudflare R2 blob host; a second attempt on another
  network completed.

## Observations from the 2026-10-30 Azure run

- `gpt-4o-mini` version 2024-07-18 is deprecated for new deployments since 2026-03-31; the
  resource was created with `gpt-4.1-mini` 2025-04-14 instead, which is already in the price
  book. The setup guide and this plan now use it.
- With the deployment at 10K tokens per minute one of the demo's 30 calls got an Azure 429
  (token rate limit), which the gateway reports as HTTP 502 `upstream_error` and does not
  record. Capacity was raised to 50K; the setup guide says so. Mapping upstream 429 to 429
  with `Retry-After` instead of 502 is a candidate improvement.
- The demo's `--model` override sends the same model to every team, so `research` and
  `customer-support`, whose allow-lists name `gpt-4o-mini` only, were rejected with 403
  `model_not_allowed`. Correct behaviour, but for a cost-focused live run either widen those
  lists in a copy of the policy file or accept the rejections.
- The spend of a 30-call run (0.0007 USD) rounds to 0.00 in Azure Cost Management. For a
  reconciliation to the cent, send a few hundred calls (300 calls are about 0.01 USD) or
  longer prompts before comparing.
- `docker-compose.yml` uses the `env_file: [{path: .env, required: false}]` form, which needs
  Docker Compose 2.24 or newer; Docker Desktop 4.25 (Compose 2.23) rejects the file. The README
  states the minimum version. The image itself was tested with `docker run --env-file .env`.
- In the 800-call run 87 calls (11 %) were throttled by Azure even at 50K tokens per minute,
  because the deployment's requests-per-minute allowance is derived from the token capacity
  (6 RPM per 1K TPM) and the demo sends calls back to back. These are now returned as `429
  upstream_rate_limited` with the provider's `Retry-After` instead of 502; the gateway's own
  per-team rate limit is the right tool to stay under the deployment's allowance.
- For the Cost Management reconciliation: `azure-live.db` holds 674 rows / 0.0584024 USD for
  2026-10-30. Four further calls were made outside that ledger (v1 endpoint smoke, two tracing
  calls, one Docker call), about 0.00005 USD, so the Azure figure should read 0.06 USD.
- The `gpt-4.1-mini-dev` deployment was created as regional `Standard`; the Retail Prices API
  lists regional gpt-4.1-mini at 0.484 / 1.936 USD per 1M tokens, 21 % above the Global
  Standard list price the price book carries (0.40 / 1.60). The 2026-10-30 usage (12,214 in,
  33,448 out) will therefore be billed about 0.071 USD against the ledger's 0.0584 USD. The
  deployment was recreated as `GlobalStandard` on 2026-10-30 at about 17:00 CET, so later
  usage is priced as the book says. `pricing sync --deployment regional` would have shown it.
