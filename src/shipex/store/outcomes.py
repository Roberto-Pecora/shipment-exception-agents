"""Agent outcomes keyed by case_id: a redelivered case reuses its first outcome.

Re-running the agent on redelivery is not safe: idempotency keys stop the *same* action
repeating, but a second run can choose a *different* one (rebook first time, claim the
next) and pays for the model twice. The first finished outcome for a case is stored here
and every redelivery republishes it. First writer wins, so concurrent runs converge too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import psycopg


@dataclass(frozen=True)
class StoredOutcome:
    event_type: str
    payload_json: str


class OutcomeStore(Protocol):
    def get(self, case_id: str) -> StoredOutcome | None: ...

    def put_if_absent(self, case_id: str, outcome: StoredOutcome) -> StoredOutcome:
        """Store `outcome` unless one exists; return whichever is stored."""
        ...


class InMemoryOutcomeStore:
    def __init__(self) -> None:
        self._rows: dict[str, StoredOutcome] = {}

    def get(self, case_id: str) -> StoredOutcome | None:
        return self._rows.get(case_id)

    def put_if_absent(self, case_id: str, outcome: StoredOutcome) -> StoredOutcome:
        return self._rows.setdefault(case_id, outcome)


class PostgresOutcomeStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS agent_outcomes (
        case_id    TEXT PRIMARY KEY,
        event_type TEXT        NOT NULL,
        payload    TEXT        NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute(self.DDL)
        conn.commit()

    def get(self, case_id: str) -> StoredOutcome | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT event_type, payload FROM agent_outcomes WHERE case_id = %s", (case_id,)
            )
            row = cur.fetchone()
        return StoredOutcome(str(row[0]), str(row[1])) if row else None

    def put_if_absent(self, case_id: str, outcome: StoredOutcome) -> StoredOutcome:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_outcomes (case_id, event_type, payload) VALUES (%s, %s, %s) "
                "ON CONFLICT (case_id) DO NOTHING",
                (case_id, outcome.event_type, outcome.payload_json),
            )
        self._conn.commit()
        stored = self.get(case_id)
        assert stored is not None
        return stored
