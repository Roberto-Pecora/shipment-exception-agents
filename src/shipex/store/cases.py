"""At most one automated case per shipment.

Without this, a DELAYED event and a later DAMAGED event for the same parcel become two
cases that can run concurrently (one in a playbook, one in the agent) and take conflicting
actions. The router opens a case when it routes an exception and the notifier closes it
when the case's final event has been written. An exception that arrives while a case is
open goes to a human instead of starting a second automated case.

The router sees each shipment's events in order (records are keyed by shipment_id), so
`open` needs no cross-process locking; the primary key makes it safe anyway.
"""

from __future__ import annotations

from typing import Protocol

import psycopg


class CaseRegistry(Protocol):
    def open(self, shipment_id: str, case_id: str) -> str | None:
        """Open `case_id` for the shipment, or return the case that is already open.

        Re-opening the same case_id (a redelivery) succeeds and returns None.
        """
        ...

    def close(self, case_id: str) -> None: ...


class InMemoryCaseRegistry:
    def __init__(self) -> None:
        self._open: dict[str, str] = {}

    def open(self, shipment_id: str, case_id: str) -> str | None:
        current = self._open.setdefault(shipment_id, case_id)
        return None if current == case_id else current

    def close(self, case_id: str) -> None:
        for shipment_id, open_case in list(self._open.items()):
            if open_case == case_id:
                del self._open[shipment_id]


class PostgresCaseRegistry:
    DDL = """
    CREATE TABLE IF NOT EXISTS open_cases (
        shipment_id TEXT PRIMARY KEY,
        case_id     TEXT        NOT NULL,
        opened_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute(self.DDL)
        conn.commit()

    def open(self, shipment_id: str, case_id: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO open_cases (shipment_id, case_id) VALUES (%s, %s) "
                "ON CONFLICT (shipment_id) DO NOTHING",
                (shipment_id, case_id),
            )
            cur.execute("SELECT case_id FROM open_cases WHERE shipment_id = %s", (shipment_id,))
            row = cur.fetchone()
        self._conn.commit()
        current = str(row[0]) if row else case_id
        return None if current == case_id else current

    def close(self, case_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM open_cases WHERE case_id = %s", (case_id,))
        self._conn.commit()
