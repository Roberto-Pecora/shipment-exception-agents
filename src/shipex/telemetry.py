"""Tracing and metrics.

Traces: OpenTelemetry, exported over OTLP only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
(otherwise the API is a no-op, so tests and evals pay nothing). The W3C `traceparent`
travels in Kafka headers, so one trace spans ingest -> router -> agent -> carrier-gateway.
LLM spans carry GenAI semantic-convention attributes (model, token counts) plus cost.

Metrics: Prometheus counters/histograms, scraped from each worker's /metrics port.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from prometheus_client import Counter, Histogram, start_http_server

tracer = trace.get_tracer("shipex")

EVENTS_CONSUMED = Counter(
    "shipex_events_consumed_total", "Records handled by a consumer", ["consumer", "outcome"]
)
HANDLER_SECONDS = Histogram(
    "shipex_handler_seconds",
    "Handler wall time per record",
    ["consumer"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
ROUTES = Counter("shipex_routes_total", "Routing decisions", ["route", "rule_id"])
AGENT_OUTCOMES = Counter("shipex_agent_outcomes_total", "Agent run outcomes", ["status"])
LLM_TOKENS = Counter("shipex_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_COST_USD = Counter("shipex_llm_cost_usd_total", "LLM spend in USD", ["model"])
GUARDRAIL_BLOCKS = Counter("shipex_guardrail_blocks_total", "Blocked tool calls", ["tool"])
DEAD_LETTERS = Counter("shipex_dead_letters_total", "Records sent to a DLQ", ["topic"])


def configure(service_name: str) -> None:
    """Install an OTLP exporter if an endpoint is configured; set up logging."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    # Imported lazily: the SDK and exporter are only needed in deployed services.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def inject_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    carrier = dict(headers or {})
    propagate.inject(carrier)
    return carrier


def extract_context(headers: Mapping[str, str]) -> Context:
    return propagate.extract(dict(headers))


def start_metrics_server(port: int) -> None:
    start_http_server(port)
