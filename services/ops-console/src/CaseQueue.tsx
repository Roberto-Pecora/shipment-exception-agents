import { useCallback, useEffect, useState } from "react";
import { type CaseView, listHeldCases, releaseCase } from "./api";
import { actionDetails, describeAction, shortId, splitReason, waitingFor } from "./format";

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Held cases, oldest first. Releasing one hands its shipment back to automation. */
export function CaseQueue() {
  const [cases, setCases] = useState<CaseView[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setCases(await listHeldCases());
      setError(null);
    } catch (e) {
      setError(message(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function release(caseId: string) {
    setConfirming(null);
    let failure: string | null = null;
    try {
      await releaseCase(caseId);
    } catch (e) {
      failure = message(e);
    }
    // Reload either way: a 404 usually means someone else already released it.
    await load();
    if (failure) setError(failure);
  }

  return (
    <main>
      <header>
        <h1>Ops queue{cases && ` (${cases.length})`}</h1>
        <button type="button" onClick={() => void load()}>
          Refresh
        </button>
      </header>
      {error && <p role="alert">{error}</p>}
      {cases === null ? (
        <p>Loading…</p>
      ) : cases.length === 0 ? (
        <p>No cases waiting for a person.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Shipment</th>
              <th>Case</th>
              <th>Waiting</th>
              <th>Why it was escalated</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => {
              const { code, detail } = splitReason(c.reason);
              return (
                <tr key={c.case_id}>
                  <td>{c.shipment_id}</td>
                  <td>
                    <code title={c.case_id}>{shortId(c.case_id)}</code>
                  </td>
                  <td title={c.opened_at ? new Date(c.opened_at).toLocaleString() : undefined}>
                    {waitingFor(c.opened_at)}
                  </td>
                  <td>
                    {code && <span className="reason-code">{code}</span>} {detail}
                    {c.actions.length > 0 && (
                      <p className="done">
                        Already done:{" "}
                        {c.actions.map((a, i) => (
                          <span key={i} title={actionDetails(a)}>
                            {i > 0 && ", "}
                            {describeAction(a)}
                          </span>
                        ))}
                      </p>
                    )}
                  </td>
                  <td>
                    {confirming === c.case_id ? (
                      <span>
                        Hand back to automation?{" "}
                        <button type="button" onClick={() => void release(c.case_id)}>
                          Confirm release
                        </button>{" "}
                        <button type="button" onClick={() => setConfirming(null)}>
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <button type="button" onClick={() => setConfirming(c.case_id)}>
                        Release
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
