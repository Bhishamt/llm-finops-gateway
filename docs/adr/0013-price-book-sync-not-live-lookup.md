# 0013. Price book sync from the Azure Retail Prices API, never a live lookup

Date: 2026-10-30
Status: Accepted

## Context

The price book is hand-maintained YAML (ADR-0002). Azure publishes every meter through the
public Retail Prices API, so the obvious question is why the gateway does not just ask it.
Three reasons came out of the first live run:

- A ledger row must be reproducible from the price book version it records. A live price at
  call time cannot be replayed a quarter later.
- The retail list is not one price per model. The same model is billed differently per
  deployment type (Global Standard, regional Standard, Data Zone; regional gpt-4.1-mini is
  21 % above global), per model version (gpt-4o 0513 versus 1120) and per token kind (input,
  cached input, output). The meter names come in several spellings and two units. That
  mapping is a judgement, and judgements belong in a reviewed file.
- A price change is a chargeback change. Finance wants to see it in a pull request, not find
  it in a report.

## Decision

`llm-finops-gateway pricing sync --region <region> [--deployment global|regional|datazone]`
downloads the region's Foundry Models meters, classifies each token meter (model, version,
input/output/cached, deployment type, price per 1M tokens), matches them to the price book
rows with `provider: azure_openai`, and prints one line per row and field: `ok`, `differs`,
`missing` (the book has no cached-input price yet), `unmatched`, or `ambiguous` (several model
versions with different prices; the row then needs `version: YYYY-MM-DD`). A row may carry
`sku: regional|datazone` when its deployment is not Global Standard. The command exits
non-zero on drift so it can run in CI; `--write` rewrites only the numbers in place, keeps
comments and layout, and bumps `version` to today, ready for a pull request.

Nothing in the request path calls the API. The gateway keeps pricing from the file it loaded
at startup.

## Consequences

- Price drift becomes a visible, reviewable diff instead of a silent gap between the ledger
  and the invoice.
- The matcher is a heuristic over Azure's meter names and will need updating when Microsoft
  renames things; the tests pin the spellings seen on 2026-10-30. An unmatched row is reported,
  never guessed.
- Other providers (OpenAI, Anthropic) can get the same command with their own source once
  they have a machine-readable list; the report and write path are source-independent.
