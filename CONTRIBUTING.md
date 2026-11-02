# Contributing to llm-finops-gateway

Thanks for considering a contribution. This document explains how the project works day to day.
Decision-making and roles are in [GOVERNANCE.md](GOVERNANCE.md); expected behaviour is in
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Before you start

- **Bugs and small fixes:** open an issue first unless the fix is trivial (typo, obvious one-liner).
- **Features and design changes:** open a *design discussion* issue describing the problem, the
  proposed approach and alternatives. Anything that changes an interface, a data model, a default
  or a security property needs an ADR (see below) before code.
- **CLA:** the first pull request from a new contributor is held until the CLA bot records your
  acceptance of [CLA.md](CLA.md). Comment the sentence the bot asks for; that is all.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,demo,otel,azure,postgres]"
pre-commit install
```

Run the full check locally before pushing; CI runs exactly these:

```bash
ruff check . && ruff format --check .
mypy src tests
pytest -q
```

Optional: `docker compose up` starts the gateway with a local Ollama for manual testing.

## Quality bar

The project is small on purpose. Keep it that way.

| Area          | Rule                                                                                   |
| ------------- | -------------------------------------------------------------------------------------- |
| Typing        | `mypy --strict` must pass. No `Any` leaking out of module boundaries, no `type: ignore` without a reason code and comment. |
| Tests         | Every module has tests. Every bug fix adds a regression test. Provider adapters are tested with `respx`, never against live APIs. |
| Style         | `ruff` (lint + format), 100-column lines. Docstrings explain *why*, not *what*. |
| Dependencies  | Adding a runtime dependency needs justification in the PR. Dev-only tools are fine. |
| Security      | No secrets in YAML, tests or fixtures. Settings use `SecretStr`. See [SECURITY.md](SECURITY.md). |
| Cost semantics| Unknown model → error. Missing attribution → error. Changing either needs an ADR. |

## Architecture decision records

Design decisions live in `docs/adr/` as numbered Markdown files
(`NNNN-short-title.md`), following the template in `docs/adr/README.md`. An ADR is required for:

- new or changed public endpoints and response shapes
- changes to the ledger schema or price book format
- new providers, policies or storage back ends
- anything that affects what a user is charged

Supersede rather than edit: a new ADR marks the old one as superseded.

## Branches, commits and pull requests

- Branch from `main`: `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `adr/<topic>`.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `feat(providers): add azure openai adapter`, `fix(pricing): reject negative token counts`.
  Keep the subject under 72 characters.
- One logical change per PR. Fill in the PR template. Link the issue.
- CI must be green. One maintainer approval is required; two for changes that touch cost
  semantics, the ledger or security.
- Maintainers squash-merge. The squash message becomes the changelog entry, so make it good.

## Changelog and releases

- Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) in the same PR
  (`Added`, `Changed`, `Fixed`, `Removed`, `Security`).
- Releases follow [Semantic Versioning](https://semver.org/). Until 1.0, minor versions may break
  the API; this is called out in the changelog.
- Maintainers tag releases (`vX.Y.Z`) from `main`; the release workflow builds and publishes.

## Reporting a security issue

Do **not** open a public issue. Follow [SECURITY.md](SECURITY.md).

## Where to ask

Open a GitHub Discussion or an issue labelled `question`. For FinOps-for-AI topics in general,
the FinOps Budapest meetup is a friendly place too.
