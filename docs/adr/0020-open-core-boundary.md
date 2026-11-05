# 0020. What stays in this repository and what an enterprise layer adds

Date: 2026-11-05
Status: Accepted

## Context

The gateway is an independent open-source project under Apache-2.0, written and maintained by
its author in private time. Service providers, the author's employer among them, build and
operate it for customers and add integration that only makes sense inside one organisation.
Without a stated boundary every such addition raises the same question twice: does it belong
upstream, and who owns it. ADR-0019 brings the LiteLLM plugin upstream, which makes the line
worth writing down.

## Decision

The dividing rule: anything that is configured from YAML and runs unchanged on a laptop, with
no knowledge of a particular tenant, organisation or finance process and no human alongside,
belongs in this repository. Anything that needs such knowledge, or a person to run it, is an
enterprise layer outside it.

In this repository (the core):

- the gateway and the LiteLLM plugin with the same rules: attribution, price book, budgets,
  rate limits, ledger, headers;
- provider adapters, streaming, tool calling, reasoning parameters;
- `pricing sync`, `ledger reconcile`, the report page, CSV and summary APIs, alerts;
- metrics, tracing, the Grafana dashboard, the Bicep reference deployment, the container
  image, the examples;
- generic identity later (OIDC issuer list and claim mapping), multi-currency price books and
  a versioned exchange-rate table, FOCUS export.

Outside it (an enterprise layer, built and sold by an integrator):

- wiring to a concrete tenant: Entra ID groups or Keycloak realms mapped to teams, network
  policies, API Management, Private Link, landing-zone conventions;
- cost-centre mapping, chargeback export to an ERP, Cost Management export pipelines,
  variance reports, BI models;
- approval workflows for budget changes, multi-tenant hosted variants, high-availability
  database operations, compliance evidence packs;
- price-book maintenance with negotiated rates, monthly reconciliation and budget reviews as
  a service, training and enablement.

Contribution rules that follow:

- Improvements to anything in the first list are contributed upstream under the project's
  CLA.
- The repository stays independent: its name, licence and maintainer do not change with a
  sponsor; sponsors are acknowledged in the README.
- A feature request that needs tenant knowledge is answered with a hook or a configuration
  point in the core, not with tenant code.

## Consequences

- Integrators can state plainly what they sell: the second list, around a core anyone can run.
- The core stays small enough to review and reason about, and does not accumulate one
  organisation's conventions.
- The rule is a judgement call at the edges (the Bicep reference deployment is in, a
  customer's landing zone is out); this ADR is the place to argue a borderline case.
