// Client for ops-api (src/shipex/services/ops_api.py). The types are generated from its
// OpenAPI spec (`make api-types`), so the pydantic models stay the only definition.

import type { components } from "./api-types";

export type CaseView = components["schemas"]["CaseView"];
/** A side effect automation already took for the case. */
export type ActionView = components["schemas"]["Action"];

async function request(path: string, init?: RequestInit): Promise<Response> {
  const response = await fetch(`/v1${path}`, init);
  if (!response.ok) {
    throw new Error(`${init?.method ?? "GET"} ${path} failed with ${response.status}`);
  }
  return response;
}

export async function listHeldCases(): Promise<CaseView[]> {
  const response = await request("/cases/held");
  return (await response.json()) as CaseView[];
}

export async function releaseCase(caseId: string): Promise<void> {
  await request(`/cases/${encodeURIComponent(caseId)}/release`, { method: "POST" });
}
