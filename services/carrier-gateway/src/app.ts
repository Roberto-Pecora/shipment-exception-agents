// carrier-gateway: the integration boundary the agent's tools call.
//
// - Read APIs: tracking and rebooking options.
// - Write APIs require an Idempotency-Key. A replay with the same key and body returns
//   the original response (Idempotent-Replayed: true); the same key with a different
//   body is rejected, so a retried or redelivered agent run can never double-book.
// - Fault injection (FAULT_RATE, LATENCY_MS) exercises the callers' retries, circuit
//   breaker and escalation paths in the docker-compose environment.
import { createHash } from "node:crypto";
import Fastify, { type FastifyInstance, type FastifyReply } from "fastify";
import { z } from "zod";
import {
  CarrierFixture,
  ClaimRequest,
  CustomsDocumentRequest,
  RebookRequest,
  type CarrierFixture as Fixture,
} from "./schemas";

export interface GatewayOptions {
  fixtures?: Record<string, Fixture>;
  faultRate?: number;
  latencyMs?: number;
  adminEnabled?: boolean;
  random?: () => number;
  logger?: boolean;
}

interface StoredResponse {
  fingerprint: string;
  status: number;
  body: unknown;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

function fingerprint(route: string, body: unknown): string {
  return createHash("sha256").update(route).update(JSON.stringify(body)).digest("hex");
}

function invalid(reply: FastifyReply, error: z.ZodError) {
  return reply.code(422).send({ error: "invalid request", detail: z.prettifyError(error) });
}

export function buildApp(options: GatewayOptions = {}): FastifyInstance {
  const app = Fastify({ logger: options.logger ?? false });
  const shipments = new Map<string, Fixture>(Object.entries(options.fixtures ?? {}));
  const idempotency = new Map<string, StoredResponse>();
  const random = options.random ?? Math.random;
  let sequence = 0;
  const nextId = (prefix: string) => `${prefix}-${String(++sequence).padStart(4, "0")}`;

  app.addHook("onRequest", async (request, reply) => {
    if (!request.url.startsWith("/v1/")) return;
    if (options.latencyMs) await sleep(options.latencyMs);
    if (options.faultRate && random() < options.faultRate) {
      return reply.code(503).header("Retry-After", "1").send({ error: "injected fault" });
    }
  });

  // Shared write path: key required, replay-safe, body-checked.
  async function idempotentWrite(
    route: string,
    key: string | undefined,
    body: unknown,
    reply: FastifyReply,
    create: () => { status: number; body: unknown },
  ) {
    if (!key) return reply.code(400).send({ error: "Idempotency-Key header is required" });
    const print = fingerprint(route, body);
    const prior = idempotency.get(key);
    if (prior) {
      if (prior.fingerprint !== print) {
        return reply.code(422).send({ error: "Idempotency-Key reused with a different request" });
      }
      return reply.code(prior.status).header("Idempotent-Replayed", "true").send(prior.body);
    }
    const result = create();
    if (result.status < 400) idempotency.set(key, { fingerprint: print, ...result });
    return reply.code(result.status).send(result.body);
  }

  app.get("/healthz", async () => ({ status: "ok", shipments: shipments.size }));

  app.get<{ Params: { id: string } }>("/v1/shipments/:id/tracking", async (request, reply) => {
    const fixture = shipments.get(request.params.id);
    if (!fixture) return reply.code(404).send({ error: "unknown shipment" });
    return fixture.tracking;
  });

  app.get<{ Params: { id: string } }>(
    "/v1/shipments/:id/rebooking-options",
    async (request, reply) => {
      const fixture = shipments.get(request.params.id);
      if (!fixture) return reply.code(404).send({ error: "unknown shipment" });
      return { options: fixture.rebooking_options };
    },
  );

  app.post("/v1/rebookings", async (request, reply) => {
    const parsed = RebookRequest.safeParse(request.body);
    if (!parsed.success) return invalid(reply, parsed.error);
    const { shipment_id, option_id } = parsed.data;
    const key = request.headers["idempotency-key"] as string | undefined;
    return idempotentWrite("rebookings", key, parsed.data, reply, () => {
      const fixture = shipments.get(shipment_id);
      if (!fixture) return { status: 404, body: { error: "unknown shipment" } };
      const option = fixture.rebooking_options.find((o) => o.option_id === option_id);
      if (!option) return { status: 422, body: { error: `unknown option ${option_id}` } };
      return {
        status: 201,
        body: { rebooking_id: nextId("rb"), shipment_id, option_id, new_eta: option.new_eta },
      };
    });
  });

  app.post("/v1/customs-document-requests", async (request, reply) => {
    const parsed = CustomsDocumentRequest.safeParse(request.body);
    if (!parsed.success) return invalid(reply, parsed.error);
    const { shipment_id, documents } = parsed.data;
    const key = request.headers["idempotency-key"] as string | undefined;
    return idempotentWrite("customs-document-requests", key, parsed.data, reply, () => {
      if (!shipments.has(shipment_id)) return { status: 404, body: { error: "unknown shipment" } };
      return { status: 201, body: { request_id: nextId("cdr"), shipment_id, documents } };
    });
  });

  app.post("/v1/claims", async (request, reply) => {
    const parsed = ClaimRequest.safeParse(request.body);
    if (!parsed.success) return invalid(reply, parsed.error);
    const { shipment_id, amount_gbp } = parsed.data;
    const key = request.headers["idempotency-key"] as string | undefined;
    return idempotentWrite("claims", key, parsed.data, reply, () => {
      if (!shipments.has(shipment_id)) return { status: 404, body: { error: "unknown shipment" } };
      return {
        status: 201,
        body: { claim_id: nextId("clm"), shipment_id, amount_gbp, status: "submitted" },
      };
    });
  });

  if (options.adminEnabled) {
    // Test/demo only: seed shipment state (used by scripts/smoke.py).
    app.put<{ Params: { id: string } }>("/v1/_admin/shipments/:id", async (request, reply) => {
      const parsed = CarrierFixture.safeParse(request.body);
      if (!parsed.success) return invalid(reply, parsed.error);
      if (parsed.data.tracking.shipment_id !== request.params.id) {
        return reply.code(422).send({ error: "path and tracking.shipment_id differ" });
      }
      shipments.set(request.params.id, parsed.data);
      return reply.code(204).send();
    });
  }

  return app;
}
