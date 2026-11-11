// Display helpers for the queue. Pure functions, so they are tested without rendering.

import type { ActionView } from "./api";

/**
 * Escalation reasons start with a code: "high_value: declared ..." or, for budgets,
 * "budget:steps exhausted ...". Free-text reasons have no code and are kept whole.
 */
export function splitReason(reason: string | null): { code: string | null; detail: string } {
  if (!reason) return { code: null, detail: "No reason recorded" };
  const match = /^([a-z][a-z0-9_]*)(?:: |:([a-z]+) )(.+)$/s.exec(reason);
  if (!match) return { code: null, detail: reason };
  return { code: match[2] ? `${match[1]}:${match[2]}` : match[1], detail: match[3] };
}

/** How long a case has waited, coarse enough to scan: "just now", "12 min", "3 h", "2 d". */
export function waitingFor(openedAt: string | null, now: number = Date.now()): string {
  if (!openedAt) return "unknown";
  const minutes = Math.floor((now - Date.parse(openedAt)) / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} h`;
  return `${Math.floor(minutes / (24 * 60))} d`;
}

/** Case ids are UUIDs; the first block is enough to tell rows apart. */
export function shortId(caseId: string): string {
  return caseId.slice(0, 8);
}

/** "rebook (RB-1042)": what was done and the carrier's reference for it. */
export function describeAction(action: ActionView): string {
  const label = action.kind.replaceAll("_", " ");
  return action.external_ref ? `${label} (${action.external_ref})` : label;
}

/** The action's parameters, one per line, for a hover tooltip. */
export function actionDetails(action: ActionView): string {
  return Object.entries(action.params ?? {})
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`)
    .join("\n");
}
