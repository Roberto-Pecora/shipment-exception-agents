import { describe, expect, it } from "vitest";
import { buildApp } from "../src/app";

const fixtures = {
  "SHP-1": {
    tracking: {
      shipment_id: "SHP-1",
      status: "DELAYED",
      location: "Leipzig hub",
      eta: "2026-11-06T09:00:00Z",
      history: ["picked up", "missed linehaul"],
      missing_customs_documents: [],
    },
    rebooking_options: [
      {
        option_id: "opt-air",
        service: "air express",
        new_eta: "2026-11-03T09:00:00Z",
        cost_delta_gbp: 95,
      },
    ],
  },
};

const rebook = (key?: string, body: object = { shipment_id: "SHP-1", option_id: "opt-air" }) => ({
  method: "POST" as const,
  url: "/v1/rebookings",
  headers: key ? { "idempotency-key": key } : {},
  payload: body,
});

describe("read APIs", () => {
  it("serves tracking and options, 404s unknown shipments", async () => {
    const app = buildApp({ fixtures });
    const tracking = await app.inject({ url: "/v1/shipments/SHP-1/tracking" });
    expect(tracking.statusCode).toBe(200);
    expect(tracking.json().location).toBe("Leipzig hub");

    const options = await app.inject({ url: "/v1/shipments/SHP-1/rebooking-options" });
    expect(options.json().options).toHaveLength(1);

    expect((await app.inject({ url: "/v1/shipments/NOPE/tracking" })).statusCode).toBe(404);
  });
});

describe("idempotent writes", () => {
  it("requires an Idempotency-Key", async () => {
    const res = await buildApp({ fixtures }).inject(rebook());
    expect(res.statusCode).toBe(400);
  });

  it("replays the original response for the same key", async () => {
    const app = buildApp({ fixtures });
    const first = await app.inject(rebook("case-1:rebook"));
    const second = await app.inject(rebook("case-1:rebook"));
    expect(first.statusCode).toBe(201);
    expect(second.statusCode).toBe(201);
    expect(second.headers["idempotent-replayed"]).toBe("true");
    expect(second.json()).toEqual(first.json());

    const other = await app.inject(rebook("case-2:rebook"));
    expect(other.json().rebooking_id).not.toBe(first.json().rebooking_id);
  });

  it("rejects a reused key with a different body", async () => {
    const app = buildApp({ fixtures });
    await app.inject(rebook("k"));
    const res = await app.inject(rebook("k", { shipment_id: "SHP-1", option_id: "opt-other" }));
    expect(res.statusCode).toBe(422);
  });

  it("validates bodies strictly", async () => {
    const app = buildApp({ fixtures });
    const extra = await app.inject(rebook("k", { shipment_id: "SHP-1", option_id: "x", force: 1 }));
    expect(extra.statusCode).toBe(422);
    const unknownOption = await app.inject(rebook("k2", { shipment_id: "SHP-1", option_id: "x" }));
    expect(unknownOption.statusCode).toBe(422);
  });

  it("creates customs document requests", async () => {
    const res = await buildApp({ fixtures }).inject({
      method: "POST",
      url: "/v1/customs-document-requests",
      headers: { "idempotency-key": "case-1:customs_docs" },
      payload: { shipment_id: "SHP-1", documents: ["commercial invoice"] },
    });
    expect(res.statusCode).toBe(201);
    expect(res.json().request_id).toBe("cdr-0001");
  });
});

describe("fault injection and admin", () => {
  it("returns 503 with Retry-After when the fault fires", async () => {
    const app = buildApp({ fixtures, faultRate: 0.5, random: () => 0.1 });
    const res = await app.inject({ url: "/v1/shipments/SHP-1/tracking" });
    expect(res.statusCode).toBe(503);
    expect(res.headers["retry-after"]).toBe("1");
    expect((await app.inject({ url: "/healthz" })).statusCode).toBe(200);
  });

  it("seeds fixtures only when admin is enabled", async () => {
    const body = { tracking: { shipment_id: "SHP-9", status: "CUSTOMS_HOLD" } };
    const off = await buildApp().inject({ method: "PUT", url: "/v1/_admin/shipments/SHP-9", payload: body });
    expect(off.statusCode).toBe(404);

    const app = buildApp({ adminEnabled: true });
    const put = await app.inject({ method: "PUT", url: "/v1/_admin/shipments/SHP-9", payload: body });
    expect(put.statusCode).toBe(204);
    expect((await app.inject({ url: "/v1/shipments/SHP-9/tracking" })).json().status).toBe(
      "CUSTOMS_HOLD",
    );
  });
});
