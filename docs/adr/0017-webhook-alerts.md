# 0017. Webhook alerts for budget and upstream events, sent by the gateway itself

Date: 2026-10-30
Status: Accepted

## Context

Budget state is visible in headers, bodies, logs, traces and metrics. None of those reach a
team lead who is not looking. The first operational request after a demo is always the same:
"tell us in Slack when a team crosses 80 %". The options were alerting on the Prometheus
metrics (Alertmanager, Azure Monitor alert rules), or having the gateway post the event
itself.

Metric-based alerting is the right long-term place for thresholds and routing, but it needs
a monitoring stack to exist and someone to write the rules. A small team that only runs the
gateway and a Slack workspace has neither on day one.

## Decision

- `LFG_ALERT_WEBHOOK_URL` enables alerts; one POST per event with a JSON body that has a
  human `text` line (Slack and Teams incoming webhooks render it as is) plus structured fields
  (`event`, `team`, `level`, `used_pct`, `spent`, `limit`, `provider`, `model`, `message`).
- Events: `budget_warn` (a team crossed its warning threshold), `budget_block` (a call was
  refused for budget), `upstream_error` and `upstream_rate_limited` (the provider failed).
- The same event for the same team, or provider, is sent once per
  `LFG_ALERT_COOLDOWN_SECONDS` (default one hour). A team at 85 % does not page on every
  call; it pages once an hour until it is back under the line or blocked.
- Delivery is a background task with a five-second timeout; a failed delivery is a warning
  in the log and never a failed request. In-flight posts are awaited on shutdown.
- No retries, no queue, no templating. Anything beyond that is the monitoring stack's job:
  the Prometheus metrics and the Grafana dashboard remain the source for SLOs and routing.

## Consequences

- A Slack or Teams channel knows about a budget event within seconds of the first call that
  triggers it, with no extra infrastructure.
- The webhook URL is a secret (Slack embeds the token in it) and is held as `SecretStr`;
  it never appears in logs or in `/healthz`.
- With several gateway replicas each one keeps its own cooldown, so an event can arrive once
  per replica; the shared state for that is the same future work as shared rate limits.
