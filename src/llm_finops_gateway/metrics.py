"""Prometheus metrics. One registry per app instance so tests can create many apps; the
LiteLLM plugin passes the process-wide default registry so that LiteLLM's own /metrics
endpoint serves the same ``lfg_*`` series the gateway exposes."""

from __future__ import annotations

from decimal import Decimal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else CollectorRegistry()
        self.requests = Counter(
            "lfg_requests_total",
            "Chat completion requests by outcome",
            ["team", "model", "outcome"],
            registry=self.registry,
        )
        self.cost = Counter(
            "lfg_cost_usd_total",
            "Attributed spend in USD",
            ["team", "project", "model"],
            registry=self.registry,
        )
        self.tokens = Counter(
            "lfg_tokens_total",
            "Tokens by direction",
            ["team", "model", "direction"],
            registry=self.registry,
        )
        self.upstream_latency = Histogram(
            "lfg_upstream_latency_seconds",
            "Provider round trip",
            ["provider", "model"],
            registry=self.registry,
            buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
        )
        self.budget_used = Gauge(
            "lfg_budget_used_ratio",
            "Month-to-date committed spend / monthly budget",
            ["team"],
            registry=self.registry,
        )

    def observe_cost(
        self, team: str, project: str, model: str, cost: Decimal, inp: int, out: int
    ) -> None:
        self.cost.labels(team, project, model).inc(float(cost))
        self.tokens.labels(team, model, "input").inc(inp)
        self.tokens.labels(team, model, "output").inc(out)

    def render(self) -> bytes:
        return generate_latest(self.registry)
