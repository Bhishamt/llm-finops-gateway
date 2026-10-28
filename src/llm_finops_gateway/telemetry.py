"""OpenTelemetry tracing, optional.

Install with ``pip install "llm-finops-gateway[otel]"`` and set ``LFG_OTEL_ENABLED=true``.
The exporter is configured through the standard ``OTEL_EXPORTER_OTLP_*`` variables, so the
same spans can go to Jaeger, Grafana Tempo, Azure Monitor (via the OTLP collector) or Langfuse
(``OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel`` plus a Basic auth
header). When the extra is not installed or the flag is off, every call here is a no-op.

Every request span carries ``finops.*`` attributes: team, project, use_case, model, key_id,
cost_usd, input_tokens, output_tokens, request_id and, when blocked, policy_violation.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

log = logging.getLogger("llm_finops_gateway.telemetry")


class Telemetry:
    def __init__(self, enabled: bool, service_name: str, exporter: Any | None = None) -> None:
        self.enabled = False
        self._trace: Any = None
        self._provider: Any = None
        if not enabled:
            return
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
        except ImportError:
            log.warning("LFG_OTEL_ENABLED=true but the [otel] extra is not installed; tracing off")
            return
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if exporter is not None:
            provider.add_span_processor(SimpleSpanProcessor(exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        self._trace = trace
        self._provider = provider
        self.enabled = True

    def instrument(self, app: FastAPI) -> None:
        if not self.enabled:
            return
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        except ImportError:
            log.warning("opentelemetry-instrumentation-fastapi missing; request spans off")
            return
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=self._provider, excluded_urls="healthz,metrics"
        )

    def annotate(self, **attrs: str | int | float | bool) -> None:
        """Attach attributes to the current request span (no-op when disabled)."""
        if not self.enabled:
            return
        span = self._trace.get_current_span()
        for k, v in attrs.items():
            span.set_attribute(f"finops.{k}", v)

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()
