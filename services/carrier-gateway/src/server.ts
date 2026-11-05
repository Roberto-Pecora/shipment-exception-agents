import { readFileSync } from "node:fs";
import { z } from "zod";
import { buildApp } from "./app";
import { CarrierFixture } from "./schemas";

function loadFixtures(path: string | undefined) {
  if (!path) return {};
  return z.record(z.string(), CarrierFixture).parse(JSON.parse(readFileSync(path, "utf8")));
}

async function main() {
  const app = buildApp({
    fixtures: loadFixtures(process.env.FIXTURES_PATH),
    faultRate: Number(process.env.FAULT_RATE ?? 0),
    latencyMs: Number(process.env.LATENCY_MS ?? 0),
    adminEnabled: process.env.ADMIN_ENABLED === "true",
    logger: true,
  });
  const shutdown = async () => {
    await app.close();
    process.exit(0);
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
  await app.listen({ host: "0.0.0.0", port: Number(process.env.PORT ?? 8081) });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
