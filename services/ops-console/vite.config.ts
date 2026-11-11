import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  // `npm run dev` against the stack from `make up`: ops-api is published on OPS_PORT (8082).
  server: { proxy: { "/v1": "http://localhost:8082" } },
  test: { environment: "jsdom" },
});
