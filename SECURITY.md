# Security policy

## Supported versions

| Version | Supported          |
| ------- | ------------------ |
| 0.2.x   | yes (current)      |
| 0.1.x   | no                 |

Until 1.0 only the latest minor release receives fixes.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Use GitHub's private vulnerability reporting ("Report a vulnerability" under the Security tab)
or email the project lead at the address listed in [MAINTAINERS.md](MAINTAINERS.md). Include:

- affected version or commit,
- a description and, if possible, a proof of concept,
- the impact you believe it has.

You will get an acknowledgement within 3 working days and a status update at least every 7 days
until the issue is resolved. We aim to release a fix within 30 days of confirmation and will
credit you in the release notes unless you prefer otherwise.

## Scope

In scope: the gateway service, its configuration loading, provider adapters, ledger and any
official container image or Bicep template in this repository.

Out of scope: vulnerabilities in upstream providers (Azure OpenAI, Ollama), in dependencies that
are already public, and findings that require a compromised host or a misconfigured deployment
contrary to the documentation.

## Handling secrets

- Provider credentials are read only from environment variables or `.env`, never from YAML or
  the ledger. `pydantic.SecretStr` keeps them out of logs and `repr`.
- Prompt and completion **content is not stored** by the ledger; only token counts,
  cost, model and attribution tags. Future tracing features will be opt-in and documented.
- `.env` and `*.db` are git-ignored. Rotate any key you suspect was committed.
- Team keys (ADR-0006) identify the calling team, not a person, and are the only built-in
  authentication, for the model endpoint and for the reports (`role: finops` reads every
  team). Run the gateway on an internal network or behind your platform's ingress auth
  (Azure Container Apps auth, API Management, an OAuth proxy); do not expose it publicly.
- On Azure the reference deployment calls Azure OpenAI with a managed identity (no provider
  key), keeps the database password, connection URLs and raw team keys in Key Vault, and
  runs the gateway against the database as a role that cannot delete (`docs/database.md`).
- Webhook URLs for alerts are secrets (`SecretStr`) and never logged.

## Dependency hygiene

Dependabot opens weekly update PRs for Python packages and GitHub Actions. CI runs `pip-audit`
on every push and fails on known vulnerabilities with a fix available.
