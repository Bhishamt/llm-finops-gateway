# 0004. Application factory with injected price book, ledger and provider

Date: 2026-11-01
Status: Accepted

## Context

The first sketch of `main.py` created the FastAPI app, the price book, the ledger and the
provider at module import time. That makes the module untestable without the real config file
and a database on disk, and it makes it impossible to run two differently configured instances
(for example in a test suite that exercises both providers).

## Decision

`create_app(settings, *, price_book, ledger, provider)` builds the application. Each collaborator
can be injected; anything not injected is built from `Settings` (which reads `LFG_*` environment
variables). The collaborators are attached to `app.state` in the lifespan handler and reached
through small FastAPI dependencies (`_price_book`, `_ledger`, `_provider`) so endpoints have no
module-level globals. The provider's HTTP client is closed on shutdown.

Uvicorn runs the app in factory mode (`uvicorn --factory llm_finops_gateway.main:create_app`)
and the `llm-finops-gateway` console script does the same.

## Consequences

- Tests inject a `FakeProvider` and an in-memory ledger and run the full HTTP stack with
  `TestClient`; no network, no files.
- Startup fails fast on bad configuration (for example `provider=azure_openai` without
  credentials) instead of on the first request.
- A future multi-tenant deployment can create one app per tenant from the same factory.
- Contributors must not reintroduce module-level singletons; the pattern is enforced by review.
