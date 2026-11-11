# 0023. The ledger is exported in the FOCUS format

Date: 2026-11-05
Status: Accepted

## Context

FinOps teams normalise every cost source into the FinOps Open Cost and Usage Specification
(FOCUS) so that cloud bills, SaaS invoices and internal chargeback can be joined in one
dataset. The ledger already has everything a FOCUS row needs (when, who, what, how much, at
which price list version), but only as the gateway's own summary CSV. A team that already
loads FOCUS exports from Azure or AWS should be able to load LLM spend the same way, without
a custom mapping.

## Decision

- `GET /v1/finops/export.focus.csv?from=YYYY-MM-DD&to=YYYY-MM-DD` returns the ledger as
  FOCUS rows, one per team, project, use case, provider, model, price book version and UTC
  day, under the same access rule as the other reports (team key sees its team, `role: finops`
  sees all). Without dates it covers the current calendar month to date.
- Columns: the FOCUS 1.1 mandatory set (`BilledCost`, `EffectiveCost`, `ListCost`,
  `ContractedCost`, `BillingCurrency`, `BillingAccountId`, `BillingAccountName`,
  `BillingPeriodStart/End`, `ChargePeriodStart/End`, `ChargeCategory`, `ChargeClass`,
  `ChargeDescription`, `ChargeFrequency`, `InvoiceIssuerName`, `ProviderName`,
  `PublisherName`, `ServiceCategory`, `ServiceName`, `SubAccountId`, `SubAccountName`,
  `ConsumedQuantity`, `ConsumedUnit`, `PricingQuantity`, `PricingUnit`, `ResourceId`,
  `ResourceName`, `ResourceType`, `SkuId`, `SkuPriceId`, `Tags`) plus `x_` extension columns
  for what FOCUS has no home for: calls, input, output and cached tokens, and the price
  book version.
- Cost semantics: the ledger records one price per call, so `BilledCost`, `EffectiveCost`,
  `ContractedCost` and `ListCost` are the same number until discounts or capacity pricing
  exist; the day they differ, this ADR is superseded. `BillingCurrency` is the price book's
  currency. Quantities are tokens: `ConsumedQuantity` and `PricingQuantity` are input plus
  output tokens, `ConsumedUnit` and `PricingUnit` are `Tokens`.
- Attribution travels in `Tags` as a JSON object (`team`, `project`, `use_case`) and in
  `SubAccountId` (the team), so both tag-based and account-based FOCUS consumers can allocate
  the rows.
- Provider names map to the FOCUS provider and service vocabulary where known (`azure_openai`
  and LiteLLM's `azure` to Microsoft and Azure OpenAI, `ollama` to a self-hosted service); an
  unknown provider is exported under its own name rather than dropped.
- The same export is available from the CLI (`ledger export --focus`) for scheduled loads.

## Consequences

- LLM spend joins the FinOps dataset with the same loader as any other FOCUS source, per day,
  with attribution preserved.
- The daily grain keeps exports small; per-call detail stays in the ledger and in the summary
  API.
- Columns that FOCUS does not define are prefixed `x_` as the specification requires, so a
  strict loader can ignore them.
