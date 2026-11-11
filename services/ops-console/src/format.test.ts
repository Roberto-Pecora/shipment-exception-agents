import { describe, expect, it } from "vitest";
import { actionDetails, describeAction, shortId, splitReason, waitingFor } from "./format";
import type { ActionView } from "./api";

describe("splitReason", () => {
  it("separates the reason code from its explanation", () => {
    expect(splitReason("high_value: declared value at or above the threshold")).toEqual({
      code: "high_value",
      detail: "declared value at or above the threshold",
    });
  });

  it("reads agent budget reasons, which have no space after the colon", () => {
    expect(splitReason("budget:steps exhausted after 8 model calls")).toEqual({
      code: "budget:steps",
      detail: "exhausted after 8 model calls",
    });
  });

  it("keeps free-text reasons whole", () => {
    expect(splitReason("needs a specialist")).toEqual({ code: null, detail: "needs a specialist" });
  });

  it("covers a missing reason", () => {
    expect(splitReason(null).detail).toBe("No reason recorded");
  });
});

describe("waitingFor", () => {
  const opened = "2026-11-10T09:00:00Z";
  const at = (iso: string) => Date.parse(iso);

  it.each([
    ["2026-11-10T09:00:30Z", "just now"],
    ["2026-11-10T09:12:00Z", "12 min"],
    ["2026-11-10T12:30:00Z", "3 h"],
    ["2026-11-12T10:00:00Z", "2 d"],
  ])("at %s reads %s", (now, expected) => {
    expect(waitingFor(opened, at(now))).toBe(expected);
  });

  it("handles a case with no timestamp", () => {
    expect(waitingFor(null)).toBe("unknown");
  });
});

it("shortId keeps the first block of a UUID", () => {
  expect(shortId("6c3e6d41-367a-5353-bdad-60b6c5ea5a51")).toBe("6c3e6d41");
});

describe("actions", () => {
  const docs: ActionView = {
    kind: "customs_docs_request",
    params: { documents: ["EORI", "invoice"] },
    external_ref: "CD-7",
  };

  it("names the action and its carrier reference", () => {
    expect(describeAction(docs)).toBe("customs docs request (CD-7)");
    expect(describeAction({ ...docs, external_ref: null })).toBe("customs docs request");
  });

  it("lists parameters for the tooltip", () => {
    expect(actionDetails(docs)).toBe("documents: EORI, invoice");
  });
});
