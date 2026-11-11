import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ActionView, CaseView } from "./api";
import { CaseQueue } from "./CaseQueue";

const HELD: CaseView = {
  shipment_id: "SHP-1",
  case_id: "case-1",
  status: "held",
  reason: "needs a specialist",
  opened_at: new Date().toISOString(),
  actions: [],
};

/** A fake ops-api: GET returns the queue, POST release removes the case. */
function fakeOpsApi(queue: CaseView[]) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      const index = queue.findIndex((c) => url.includes(c.case_id));
      if (index === -1) return new Response(null, { status: 404 });
      queue.splice(index, 1);
      return new Response(null, { status: 204 });
    }
    return Response.json(queue);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("CaseQueue", () => {
  it("shows held cases with the reason they were escalated", async () => {
    fakeOpsApi([HELD]);
    render(<CaseQueue />);
    expect(await screen.findByText("needs a specialist")).toBeTruthy();
    expect(screen.getByText("SHP-1")).toBeTruthy();
    expect(screen.getByText("Ops queue (1)")).toBeTruthy();
    expect(screen.getByText("just now")).toBeTruthy();
  });

  it("shows what automation already did, so ops does not repeat it", async () => {
    const rebook: ActionView = {
      kind: "rebook",
      params: { option_id: "opt-air" },
      external_ref: "RB-1",
    };
    fakeOpsApi([{ ...HELD, actions: [rebook] }]);
    render(<CaseQueue />);
    expect((await screen.findByText(/Already done/)).textContent).toBe(
      "Already done: rebook (RB-1)",
    );
  });

  it("says so when nothing is waiting", async () => {
    fakeOpsApi([]);
    render(<CaseQueue />);
    expect(await screen.findByText("No cases waiting for a person.")).toBeTruthy();
  });

  it("releases only after confirmation, then reloads the queue", async () => {
    const fetchMock = fakeOpsApi([{ ...HELD }]);
    render(<CaseQueue />);
    fireEvent.click(await screen.findByText("Release"));
    expect(fetchMock).not.toHaveBeenCalledWith(expect.anything(), { method: "POST" });

    fireEvent.click(screen.getByText("Confirm release"));
    await waitFor(() => screen.getByText("No cases waiting for a person."));
    expect(fetchMock).toHaveBeenCalledWith("/v1/cases/case-1/release", { method: "POST" });
  });

  it("shows the error and reloads when a release fails", async () => {
    const fetchMock = fakeOpsApi([{ ...HELD }]);
    render(<CaseQueue />);
    fireEvent.click(await screen.findByText("Release"));
    fetchMock.mockImplementationOnce(async () => new Response(null, { status: 500 }));
    fireEvent.click(screen.getByText("Confirm release"));
    expect((await screen.findByRole("alert")).textContent).toContain("failed with 500");
    expect(screen.getByText("SHP-1")).toBeTruthy();
  });
});
