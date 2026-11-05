"""Ingest ledger: event id -> content fingerprint.

Deduping on the carrier's event id alone silently drops a *correction* (same id,
different content). Storing a fingerprint lets ingest tell a harmless re-send (same id,
same content: accepted as a duplicate) from a conflicting one (same id, different
content: rejected with 409 so the carrier or an operator sees it).
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import psycopg

from shipex.contracts.events import ShipmentEvent


def fingerprint(event: ShipmentEvent) -> str:
    return hashlib.sha256(event.model_dump_json().encode("utf-8")).hexdigest()


class IngestLedger(Protocol):
    def lookup(self, event_id: str) -> str | None:
        """The stored fingerprint for this event id, if it was ingested before."""
        ...

    def record(self, event_id: str, fingerprint: str) -> None: ...


class InMemoryIngestLedger:
    def __init__(self) -> None:
        self._rows: dict[str, str] = {}

    def lookup(self, event_id: str) -> str | None:
        return self._rows.get(event_id)

    def record(self, event_id: str, fingerprint: str) -> None:
        self._rows.setdefault(event_id, fingerprint)


class PostgresIngestLedger:
    DDL = """
    CREATE TABLE IF NOT EXISTS ingested_events (
        event_id    TEXT PRIMARY KEY,
        fingerprint TEXT        NOT NULL,
        received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute(self.DDL)
        conn.commit()

    def lookup(self, event_id: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT fingerprint FROM ingested_events WHERE event_id = %s", (event_id,))
            row = cur.fetchone()
        return str(row[0]) if row else None

    def record(self, event_id: str, fingerprint: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingested_events (event_id, fingerprint) VALUES (%s, %s) "
                "ON CONFLICT (event_id) DO NOTHING",
                (event_id, fingerprint),
            )
        self._conn.commit()
