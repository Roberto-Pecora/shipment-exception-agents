"""Write-ahead journal of agent side effects, keyed by case_id.

Stored outcomes (store/outcomes.py) cover a crash *after* an agent run finishes. This
covers a crash *during* one. Every side effect is journalled as an intent before the
carrier is called and marked done with its external reference afterwards. If a case is
redelivered with journal entries but no stored outcome, the previous run was interrupted
after it started acting, so the agent is not re-run: the case escalates to a person with
the confirmed and unconfirmed actions, and a second run can never pick a different action.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg


@dataclass(frozen=True)
class JournalEntry:
    idempotency_key: str
    kind: str
    params: dict[str, Any] = field(default_factory=dict)
    external_ref: str | None = None  # None: intent recorded, outcome unknown


class ActionJournal(Protocol):
    def record_intent(
        self, case_id: str, idempotency_key: str, kind: str, params: dict[str, Any]
    ) -> None: ...

    def record_done(self, case_id: str, idempotency_key: str, external_ref: str) -> None: ...

    def entries(self, case_id: str) -> list[JournalEntry]: ...


class InMemoryActionJournal:
    def __init__(self) -> None:
        self._rows: dict[str, dict[str, JournalEntry]] = {}

    def record_intent(
        self, case_id: str, idempotency_key: str, kind: str, params: dict[str, Any]
    ) -> None:
        self._rows.setdefault(case_id, {}).setdefault(
            idempotency_key, JournalEntry(idempotency_key, kind, dict(params))
        )

    def record_done(self, case_id: str, idempotency_key: str, external_ref: str) -> None:
        entry = self._rows[case_id][idempotency_key]
        self._rows[case_id][idempotency_key] = JournalEntry(
            entry.idempotency_key, entry.kind, entry.params, external_ref
        )

    def entries(self, case_id: str) -> list[JournalEntry]:
        return list(self._rows.get(case_id, {}).values())


class PostgresActionJournal:
    DDL = """
    CREATE TABLE IF NOT EXISTS agent_action_journal (
        case_id         TEXT        NOT NULL,
        idempotency_key TEXT        NOT NULL,
        kind            TEXT        NOT NULL,
        params          TEXT        NOT NULL,
        external_ref    TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at    TIMESTAMPTZ,
        PRIMARY KEY (case_id, idempotency_key)
    )
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute(self.DDL)
        conn.commit()

    def record_intent(
        self, case_id: str, idempotency_key: str, kind: str, params: dict[str, Any]
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_action_journal (case_id, idempotency_key, kind, params) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (case_id, idempotency_key) DO NOTHING",
                (case_id, idempotency_key, kind, json.dumps(params, default=str)),
            )
        self._conn.commit()

    def record_done(self, case_id: str, idempotency_key: str, external_ref: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_action_journal SET external_ref = %s, completed_at = now() "
                "WHERE case_id = %s AND idempotency_key = %s",
                (external_ref, case_id, idempotency_key),
            )
        self._conn.commit()

    def entries(self, case_id: str) -> list[JournalEntry]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT idempotency_key, kind, params, external_ref FROM agent_action_journal "
                "WHERE case_id = %s ORDER BY created_at",
                (case_id,),
            )
            rows = cur.fetchall()
        return [
            JournalEntry(str(r[0]), str(r[1]), json.loads(str(r[2])), str(r[3]) if r[3] else None)
            for r in rows
        ]
