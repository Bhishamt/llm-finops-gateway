# 0009. Evaluation replay: re-run recorded traffic to score quality and cost together

Date: 2026-10-28
Status: Proposed (design only; implementation is the next milestone)

## Context

Every model change is two decisions at once: does quality hold, and what does it do to the
bill. Today those are measured by different people with different tools, usually after the
change. The gateway already sees every call with its attribution, tokens and cost; it is the
natural place to turn a sample of real traffic into a repeatable regression suite.

Constraints that shape the design:

- The ledger stores no prompt or completion text (SECURITY.md). Replay therefore needs an
  explicit, opt-in capture of samples, never a silent copy of production traffic.
- Scoring must be cheap enough to run on a laptop and honest enough to be shown to finance and
  to a model-governance board.
- The result has to be one table, not two tools.

## Decision (proposed)

1. **Capture is opt-in per team and sampled.** `policies.yaml` gains
   `evaluation.capture: { sample_rate: 0.02, max_per_day: 200, redact: true }`. Captured
   samples (messages, parameters, response, usage, cost, attribution, model, price book version)
   go to a separate `eval_samples` table or a JSONL file under a configurable path. Redaction
   runs the same PII policy that is planned for requests. Capture is logged and visible in
   `/v1/finops/budgets` so nobody is surprised.

2. **A suite is a YAML file in git**, like prices and policies:
   ```yaml
   version: "2026-11"
   source: { team: customer-support, since: 2026-11-01, limit: 200 }   # or a JSONL path
   candidates: ["gpt-4o-mini", "gpt-4.1-mini", "llama3.1:8b"]
   baseline: "gpt-4o-mini"
   scorers: [exact_match, json_valid, llm_judge]
   judge: { model: "gpt-4o", rubric: docs/eval/rubric-support.md }
   ```
3. **`llm-finops-gateway eval run suite.yaml`** replays every sample against every candidate
   *through the gateway itself* (so the runs are attributed to an `evaluation` team with its
   own budget and show up in chargeback), scores them, and writes `eval/out/<suite>-<date>/`
   with `report.md`, `results.jsonl` and a chart.

4. **One table per candidate**: quality score (per scorer, with confidence interval from the
   sample size), p50/p95 latency, mean cost per call, projected monthly cost at the source
   team's current volume, and the delta against the baseline. A `--fail-on` option
   (`quality_drop > 0.02 or cost_increase > 0.10`) makes it usable in CI.

5. **Scorers are pluggable** via a small `Scorer` protocol; the built-ins are deterministic
   (`exact_match`, `contains`, `json_valid`, `regex`) plus `llm_judge` with a rubric file. The
   judge's cost is itself attributed and shown in the report, because evaluating is spending.

## Consequences

- Model migrations become a reviewed artefact: the suite file and the report go into the PR
  that changes `allowed_models` or the default model.
- Capture is a data-protection decision. It is off by default, per team, sampled, redacted,
  and documented in SECURITY.md once implemented.
- The judge model introduces its own bias and cost; the report always shows deterministic
  scorers next to it and never a single blended number.
- Replay through the gateway means the evaluation team needs a budget; running out of it is a
  feature.
- Out of scope for the first version: streaming samples, multi-turn conversations beyond what
  was captured, and statistical significance beyond a simple bootstrap interval.
