# CLAUDE.md

Project context for Claude Code. This file is committed and will be public once the repository
is; keep it free of anything private.

## What this is

`llm-finops-gateway`: an OpenAI-compatible LLM gateway that makes every call attributable
(team + project, optional use case), prices it from a versioned price book, enforces per-team
budgets and rate limits as code, and exposes cost and policy data to Prometheus / OpenTelemetry.
Apache-2.0, maintained by András Kelle (`andrasKelle`). Version `0.3.1` (2026-11-12), first commit pushed
2026-10-28, repository private until launch.

Audience: platform / FinOps teams that put the gateway in front of Azure OpenAI, Ollama (on-prem)
and any OpenAI-compatible endpoint (Azure OpenAI v1, vLLM, LocalAI). Positioning vs LiteLLM, Portkey, Bifrost, Helicone is
in `docs/comparison.md`: attribution is a precondition, price book is data in git, unknown
models fail closed, policy is reviewable YAML.

## Layout

```
src/llm_finops_gateway/
  main.py          create_app(settings, price_book, policies, keyring, ledger, provider, providers);
                   endpoints, build_providers(), route_models() (ADR-0011)
  config.py        Settings (env prefix LFG_): provider, auth_mode, paths, database_url, otel_*
  pricing.py       PriceBook (config/pricing.yaml, versioned), UnknownModelError, money()
  pricing_sync.py  `pricing sync`: Azure Retail Prices meters -> price book diff / rewrite (ADR-0013)
  policy.py        PolicySet / PolicyEngine: allow-list -> rate limit -> atomic budget reservation
  ledger.py        SQLAlchemy ledger (cost_records, budget_counters), reserve/settle/release
  auth.py          KeyRing (SHA-256 hashes from config/keys.yaml), generate_key(), hash_key()
  attribution.py   Attribution(team, project, use_case, key_id); api_key vs headers mode
  enforcement.py   rejection() and the X-FinOps-* header builders shared by main.py and the plugin
  reload.py        WatchedFile: price book and policies reloaded on change (ADR-0022)
  focus.py         FOCUS 1.1 export of the ledger, daily rows (ADR-0023); CLI `ledger export`
  litellm_plugin.py FinOpsPlugin (LiteLLM CustomLogger): the same rules inside a LiteLLM proxy (ADR-0019)
  litellm_ui.py    the report page as a LiteLLM dashboard plugin: claim verification, session cookie, shell (ADR-0024)
  providers/       base.py (Provider protocol), ollama.py, azure_openai.py, openai_compat.py (ADR-0010),
                   azure_identity.py (Entra ID tokens, [azure] extra, ADR-0014)
  metrics.py       Prometheus; telemetry.py optional OTel ([otel] extra, finops.* span attrs)
  models.py        request/response models (OpenAI shape + x_finops block)
  cli.py           llm-finops-gateway serve | keys new|hash|check | pricing sync | alerts test | ledger reconcile|export
  reconcile.py     ledger vs Azure Cost Management per model and day (ADR-0018)
  report.py        /finops HTML page, summary CSV, report access scope (team key vs role finops, ADR-0016)
  alerts.py        Alerter: webhook POST per budget/upstream event, cooldown, background delivery (ADR-0017)
config/            pricing.yaml, policies.yaml, keys.yaml (hashes only, repo copy is `keys: []`)
demo/              run_demo.py (simulated or live), policies.yaml (tiny budgets), README.md
examples/          OpenAI SDK examples (python/chat.py, typescript/chat.ts); litellm/ compose stack,
                   Dockerfile and demo.sh for the plugin; assistant/ Streamlit page (key sign-in, receipt,
                   docs Q&A through the gateway); grafana/ dashboard JSON
deploy/azure/      main.bicep + openai-role.bicep + keys.sh + db_roles.py + README: Container Apps + PostgreSQL + Key Vault (keys.yaml there is git-ignored);
                   litellm.bicep + litellm-deploy.sh + push-config.sh + litellm.config.yaml: the LiteLLM direction next to it
docs/database.md   ledger engines, schema, PostgreSQL roles (lfgadmin/lfg_app/lfg_reporting), operations
docs/              architecture.md, deployment.md, azure-setup.md, testing-plan.md, comparison.md,
                   litellm.md, adr/0001..0024
tests/             one test module per source module, 236 tests (+3 PostgreSQL tests when LFG_TEST_DATABASE_URL is set;
                   the 15 LiteLLM plugin tests skip without the [litellm] extra), FakeProvider in conftest.py
```

## Behaviour that must not regress

- Attribution is mandatory. `X-Project` always; team from the team-bound key
  (`Authorization: Bearer lfg_...`, default `LFG_AUTH_MODE=api_key`) or from `X-Team` in
  `headers` mode. `X-Team` that contradicts the key -> 403 `team_mismatch`. Missing -> 400
  `missing_attribution`, missing/invalid key -> 401 with `WWW-Authenticate`.
- Unknown model -> 422 before any provider call (ADR-0002). No price, no call.
- Project registry (ADR-0021): a team with a `projects` map rejects an unregistered project
  with 403 `project_not_registered` and a use case outside `use_cases` with 403
  `use_case_not_registered`, before any provider call; a project budget is reserved and
  settled next to the team's under the counter scope `team/project`; teams without the map
  keep free-text projects.
- Budget: `monthly_usd`, `warn_at` (0.8), `block_at` (1.0), `enforcement: block|warn`,
  `reserve_output_tokens`. Estimated cost is reserved atomically in `budget_counters` before the
  upstream call, settled with the ledger row in one transaction, released on failure
  (ADR-0007). Warn -> 200 with `X-FinOps-Warning` and `x_finops.policy.budget.level: warn`;
  block -> 403 `budget_exceeded`. Rate limit runs before the budget check -> 429 with
  `Retry-After` (ADR-0008, per instance, in memory).
- Every money string goes through `pricing.money()` (Decimal, no `0.0`, no `0.00`). Amounts are
  in the price book's `currency` (validated 3-letter code), echoed as `x_finops.currency` and
  `X-FinOps-Currency`; the `*_usd` field names are kept until 1.0.
- Cached prompt tokens (`usage.prompt_tokens_details.cached_tokens`) are billed at the row's
  `cached_input` price and stored on the ledger row; an upstream 429 is passed through as
  `429 upstream_rate_limited` with `Retry-After`, never as 502.
- A price book row's `reasoning: true` turns the client's `max_tokens` into
  `max_completion_tokens` and drops `temperature` before the upstream call.
- A price book row's `provider` picks the adapter; `LFG_PROVIDER` is only the default
  (ADR-0011). An unconfigured provider falls back to the default with a warning, not an error.
- Response always carries `X-FinOps-Request-Id`, `X-FinOps-Cost-USD`,
  `X-FinOps-Cached-Input-Tokens`, `X-FinOps-Budget-*`
  and the `x_finops` block (cost, price_book_version, policy, warnings, key_id).
- `ChatRequest` is `extra="forbid"`: unknown fields are 422, listed fields pass through to the
  provider as `extra`; tool calls, content parts and tool messages are forwarded unchanged
  (ADR-0015). Adding a field means adding it to `PASSTHROUGH`, nothing else.
- Streaming (`stream: true`) runs the same policy path, then settles the cost from the final
  usage chunk; the `x_finops` block is in the last SSE event. No usage -> billed at the
  estimate with `usage_estimated: true`; mid-stream failure -> reservation released, error
  event, no ledger row (ADR-0012).
- Endpoints: `/v1/chat/completions`, `/v1/models`, `/v1/finops/summary`, `/v1/finops/summary.csv`,
  `/v1/finops/export.focus.csv` (FOCUS rows per day, ADR-0023), `/v1/finops/budgets`, `/finops` (HTML),
  `/metrics`, `/healthz`; with `LFG_LITELLM_PLUGIN_NAME` + `LFG_LITELLM_SALT_KEY`: `/`, `/api/plugin-manifest`,
  `/api/plugin-auth`, `/api/plugin-session` (ADR-0024). Report views share one scope
  rule: team key = own team, `role: finops` = all, 401 without a key in `api_key` mode.

## Working rules

Bias toward caution over speed; for trivial tasks use judgment.

1. **Think before coding.** State assumptions; if several readings exist, present them instead
   of picking one silently. If a simpler approach exists, say so and push back. If something is
   unclear, stop and ask before writing code.
2. **Simplicity first.** Minimum code that solves the request. No speculative features,
   abstractions for single-use code, configurability nobody asked for, or error handling for
   impossible cases. If 200 lines could be 50, rewrite.
3. **Surgical changes.** Touch only what the request needs. Do not improve adjacent code,
   comments or formatting; match existing style. Remove only the imports, variables and
   functions that your own change made unused; mention pre-existing dead code, do not delete it.
   Every changed line should trace to the request.
4. **Goal-driven execution.** Turn each task into a verifiable goal and loop until it holds:
   - a bug: a test in the matching `tests/test_*.py` that reproduces it, then make it pass;
   - new behaviour: tests first, then code, then `make check` green;
   - a refactor: `make check` green before and after, no behaviour change in `x_finops`,
     headers or status codes;
   - a design choice: an ADR in `docs/adr/` in the same change.
   For multi-step work state a short plan as `step -> verify: check` lines before starting.

## Conventions

- `make check` must be green before any commit: ruff (lint + format), mypy `--strict` on
  `src` and `tests`, pytest. Pre-commit is installed by `make install`.
- Every module has a test module; new behaviour comes with a test in the same change.
- Every design decision gets an ADR in `docs/adr/` (next number: 0025). ADR-0009 (evaluation
  replay) is Proposed, not implemented.
- Decimal for money everywhere; never float.
- Secrets only via env / `SecretStr`. Never commit a raw `lfg_` key, `.env`, `*.db`, `demo/out/`.
- Commit messages: short, Conventional Commits (`feat:`, `fix:`, `docs:`, `ci:`). No
  `Co-Authored-By` or other AI attribution trailers.
- Work on branches and open PRs against `main`; `main` is protected once CI is stable.
  Merged head branches are deleted automatically by GitHub.
- No em dashes in prose (README, docs, ADRs); rephrase instead.
- `serve` reads `config/` relative to the working directory, or `LFG_PRICING_PATH`,
  `LFG_POLICIES_PATH`, `LFG_KEYS_PATH`. The price book and the policy file are reloaded on
  change (ADR-0022); the key file is not. The venv and the config must be in the same checkout.

## Running

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,demo]"
make check
python demo/run_demo.py                              # simulated, writes demo/out/
llm-finops-gateway keys new --team platform --id platform-main   # prints raw key once + YAML entry
llm-finops-gateway keys check
LFG_PROVIDER=ollama llm-finops-gateway serve         # http://127.0.0.1:8080
```

LiteLLM plugin: `pip install -e ".[litellm]"` needs Python <= 3.13 (no LiteLLM wheel for 3.15 yet);
`.venv313` is the local venv for it, CI covers it on 3.11 and 3.12. `examples/litellm/demo.sh` walks
the six calls against the compose stack.

Azure OpenAI: `LFG_PROVIDER=azure_openai` plus `LFG_AZURE_OPENAI_ENDPOINT`,
`LFG_AZURE_OPENAI_API_KEY`, `LFG_AZURE_OPENAI_DEPLOYMENTS` (model -> deployment), see
`docs/azure-setup.md`. Without a key: `pip install -e ".[azure]"`, `LFG_AZURE_OPENAI_AUTH=entra`
(ADR-0014). Tracing: `pip install -e ".[otel]"`, `LFG_OTEL_ENABLED=true`.

## Test status (docs/testing-plan.md)

Passed 2026-10-28 on macOS with `llama3.2:3b`: sections 0 to 3c (unit suite, simulated demo,
Ollama, budget warn/block, team keys, rate limit). Section 4 Azure OpenAI passed 2026-10-30
(`gpt-4.1-mini-dev` in Sweden Central, both adapters); 4.5 reconciled on 2026-10-31: tokens
match except 774 off-gateway test tokens, cost matches once the regional SKU is priced. 3d and
5 passed on 2026-10-30.

## CI and release state

Workflows: `ci` (ruff, mypy, pytest on 3.11 and 3.12, pip-audit, Docker build), `cla`,
`release` (tag `v*.*.*` -> wheel + sdist + GHCR image + GitHub Release), Dependabot (pip,
github-actions grouped into one weekly PR, docker).

Fixed and merged on 2026-10-28 (PRs #6 and #8), all green since:

- `audit` runs `pip install pip-audit` and `pip-audit --strict .` (project-path mode: audits
  the dependencies declared in `pyproject.toml` without installing the package). Do not switch
  back to auditing the installed environment: with `--strict`, both `--skip-editable` and a
  package that is not on PyPI count as a collection failure and fail the job.
- `cla` skips Dependabot (`if: github.actor != 'dependabot[bot]'`) because `pull_request_target`
  from Dependabot gets no secrets. The bot writes signatures to the orphan branch
  `cla-signatures` (one file, `.cla-signatures/v1/cla.json`). Never merge or delete that branch.
- `release` derives a lowercase image name (`ghcr.io/andraskelle/llm-finops-gateway`) in a
  step before the build; `github.repository` keeps the owner's casing and GHCR rejects it.
- `v0.1.0` was released on 2026-10-28 with this workflow (tag re-pointed to the fixed `main`;
  no release had been published before). Assets: `llm_finops_gateway-0.1.0-py3-none-any.whl`,
  `llm_finops_gateway-0.1.0.tar.gz`; image tags `v0.1.0` and `latest`. The image is private
  until launch (package visibility is set separately from the repository).
- `v0.2.0` released on 2026-11-02 with the same workflow: release PR #29 merged into `main`
  (2f8d75c), tag `v0.2.0` on that merge commit, release run green on the first try. Assets:
  `llm_finops_gateway-0.2.0-py3-none-any.whl`, `llm_finops_gateway-0.2.0.tar.gz`; image tags
  `v0.2.0` and `latest`. The Azure Container App `lfg-gateway` runs the `v0.2.0` image
  (built with `az acr build`, revision 4, `/healthz` reports 0.2.0).
- `v0.3.0` (2026-11-11): the LiteLLM release (ADRs 0019 to 0024), same workflow; the four Azure
  Container Apps (`lfg-gateway`, `lfg-report`, `lfg-litellm`, `lfg-assistant`) are moved to the
  `v0.3.0` images built with `az acr build` from the tag.
- `v0.3.1` (2026-11-12): patch for the version string (`__version__` in `__init__.py` is bumped by
  hand at every release; `test_healthz` now checks it against `pyproject.toml`); the four apps run `v0.3.1`.

Repository settings: Workflow permissions "Read and write" (needed by `cla` and `release`);
automatic deletion of merged head branches is on; Discussions and topics are not set yet;
branch protection is not available while the repository is private on the free plan, enable
it at launch.
