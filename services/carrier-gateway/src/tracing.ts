// Preloaded with `node --require ./dist/tracing.js` so HTTP is instrumented before
// Fastify loads. Incoming `traceparent` headers from the Python agent join its trace.
// No-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { HttpInstrumentation } from "@opentelemetry/instrumentation-http";
import { NodeSDK } from "@opentelemetry/sdk-node";

if (process.env.OTEL_EXPORTER_OTLP_ENDPOINT) {
  const sdk = new NodeSDK({
    serviceName: process.env.OTEL_SERVICE_NAME ?? "carrier-gateway",
    traceExporter: new OTLPTraceExporter(),
    instrumentations: [new HttpInstrumentation()],
  });
  sdk.start();
  process.once("SIGTERM", () => {
    void sdk.shutdown();
  });
}
