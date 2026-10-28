# Project governance

## Purpose and scope

llm-finops-gateway exists to make LLM usage attributable, governable and observable through an
OpenAI-compatible proxy. The project welcomes features that serve that purpose and declines ones
that turn it into a general application framework, an agent runtime or a vendor-specific SDK.

## Roles

### Users

Anyone who runs the gateway. Users are encouraged to file issues, join discussions and share
how they deploy it.

### Contributors

Anyone whose pull request has been merged. Contributors are credited in Git history and release
notes. There is no minimum commitment.

### Maintainers

Maintainers review and merge pull requests, triage issues, cut releases and vote on decisions.
Current maintainers are listed in [MAINTAINERS.md](MAINTAINERS.md).

**Becoming a maintainer.** A contributor with a sustained record of high-quality contributions
(code, review, documentation) over roughly three months may be nominated by an existing
maintainer. Nomination is a public issue; it passes with approval from a majority of existing
maintainers and no objection within 7 days.

**Stepping down.** Maintainers may step down at any time by PR to `MAINTAINERS.md`. A maintainer
with no activity for 6 months is moved to *emeritus* after a courtesy ping.

### Project lead

The project lead (currently the founding maintainer, Andras Kelle) holds administrative rights on
the repository and the release pipeline, is the point of contact for the CLA and security
reports, and breaks ties as described below. The lead role can be transferred by majority vote of
maintainers.

## How decisions are made

The project runs on **lazy consensus** and **documented decisions**.

1. **Routine changes** (bug fixes, docs, refactors that do not change behaviour): one maintainer
   approval on the PR.
2. **Significant changes** (new endpoints, provider adapters, ledger or price book schema,
   policies, anything affecting what a user is charged, dependency additions, security
   properties): an ADR in `docs/adr/` is opened as a PR. It stays open for at least 72 hours.
   It is accepted when it has approval from two maintainers and no unresolved objection.
3. **Governance changes** (this document, `MAINTAINERS.md`, licence, CLA, code of conduct):
   majority vote of maintainers, 7-day comment period.

Objections must state a concrete reason and, where possible, an alternative. If consensus cannot
be reached after a good-faith attempt, the project lead decides and records the rationale in the
ADR or issue.

## Releases

Maintainers cut releases from `main` following semantic versioning. A release requires green CI,
an updated `CHANGELOG.md` and, for minor and major versions, a short release note describing
breaking changes and migration steps.

## Licensing and intellectual property

The project is licensed under Apache License 2.0. All contributions are accepted under the
[Contributor License Agreement](CLA.md). The licence may only be changed to another
OSI-approved licence by unanimous vote of maintainers.

## Code of conduct

All participation is subject to the [Code of Conduct](CODE_OF_CONDUCT.md). Enforcement is
handled by the maintainers as described there.

## Security

Vulnerabilities are handled privately as described in [SECURITY.md](SECURITY.md). Maintainers
coordinate fixes and disclosure.

## Changes to this document

Propose changes by pull request. See *Governance changes* above.
