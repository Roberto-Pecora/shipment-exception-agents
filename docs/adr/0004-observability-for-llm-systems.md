# ADR 0004 — Observability for LLM systems

**Status:** accepted

## Context
An agent case crosses five processes in two languages. Debugging "why was this shipment
rebooked?" needs one trace; running the system needs cost and reliability signals.

## Decision
- **OpenTelemetry traces**, exported over OTLP to Jaeger. The W3C `traceparent` is
  injected into Kafka record headers on publish and extracted on consume, and sent on
  carrier-gateway calls (the Node service is instrumented with the OTel HTTP
  instrumentation). One trace spans ingest → router → agent → carrier-gateway → notifier.
- **GenAI attributes** on each model call span (`gen_ai.request.model`,
  `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`) plus cost; tool executions are
  `execute_tool <name>` spans; routing spans carry `shipex.rule_id`.
- **Prometheus metrics**: records by consumer and outcome (processed / duplicate /
  dead-lettered), handler latency, routes by rule, agent outcomes, LLM tokens and spend,
  guardrail blocks by tool, DLQ writes.
- Telemetry is a no-op unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so tests and evals
  pay nothing; one test installs an in-memory exporter and asserts a single trace id
  across the whole pipeline.

## Consequences
- Alerts worth wiring next: DLQ writes > 0, escalation rate above baseline, LLM spend
  per hour, p95 agent handler latency.
- Prompts and full transcripts are not exported as span events (PII and volume); the
  agent's tool-call transcript is kept on the outcome for evals instead.
