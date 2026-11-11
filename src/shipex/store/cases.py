"""At most one case per shipment, automated or held by ops.

Without this, a DELAYED event and a later DAMAGED event for the same parcel become two
cases that can run concurrently (one in a playbook, one in the agent) and take conflicting
actions. The router opens a case when it routes an exception. When the case's final event
is written, the notifier either closes it (resolved) or puts it on hold (escalated): a
held case belongs to a person and stays open until they release it through ops-api. A
held case keeps the actions automation already took, so that person does not repeat them. Any
exception that arrives while a case is open or held goes to a human instead of starting a
second case.

The router sees each shipment's events in order (records are keyed by shipment_id), so
`open` needs no cross-process locking; the primary key makes it safe anyway.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol

import psycopg

from shipex.contracts.events import Action

Status = Literal["open", "held"]


@dataclass(frozen=True)
class OpenCase:
    shipment_id: str
    case_id: str
    status: Status
    reason: str | None = None
    opened_at: datetime | None = None
    actions: tuple[Action, ...] = ()  # taken before escalation (e.g. a rebook)


class CaseRegistry(Protocol):
    def open(self, shipment_id: str, case_id: str) -> OpenCase | None:
        """Open `case_id` for the shipment, or return the case that is already there.

        Re-opening the same case_id (a redelivery) succeeds and returns None.
        """
        ...

    def close(self, case_id: str) -> None: ...

    def hold(self, case_id: str, reason: str, actions: tuple[Action, ...] = ()) -> None:
        """Hand the case to ops: it stays open until `release`. No-op if not registered."""
        ...

    def release(self, case_id: str) -> bool:
        """Ops is done with a held case. True if a held case was released."""
        ...

    def get(self, shipment_id: str) -> OpenCase | None: ...

    def list_held(self) -> list[OpenCase]:
        """Cases waiting for a person, oldest first: the ops queue."""
        ...


class InMemoryCaseRegistry:
    def __init__(self) -> None:
        self._cases: dict[str, OpenCase] = {}

    def open(self, shipment_id: str, case_id: str) -> OpenCase | None:
        current = self._cases.setdefault(
            shipment_id, OpenCase(shipment_id, case_id, "open", opened_at=datetime.now(UTC))
        )
        return None if current.case_id == case_id else current

    def close(self, case_id: str) -> None:
        for shipment_id, case in list(self._cases.items()):
            if case.case_id == case_id:
                del self._cases[shipment_id]

    def hold(self, case_id: str, reason: str, actions: tuple[Action, ...] = ()) -> None:
        for shipment_id, case in list(self._cases.items()):
            if case.case_id == case_id:
                self._cases[shipment_id] = replace(
                    case, status="held", reason=reason, actions=actions
                )

    def release(self, case_id: str) -> bool:
        for shipment_id, case in list(self._cases.items()):
            if case.case_id == case_id and case.status == "held":
                del self._cases[shipment_id]
                return True
        return False

    def get(self, shipment_id: str) -> OpenCase | None:
        return self._cases.get(shipment_id)

    def list_held(self) -> list[OpenCase]:
        return [case for case in self._cases.values() if case.status == "held"]


class PostgresCaseRegistry:
    DDL = (
        """
        CREATE TABLE IF NOT EXISTS open_cases (
            shipment_id TEXT PRIMARY KEY,
            case_id     TEXT        NOT NULL,
            opened_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "ALTER TABLE open_cases ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open'",
        "ALTER TABLE open_cases ADD COLUMN IF NOT EXISTS reason TEXT",
        "ALTER TABLE open_cases ADD COLUMN IF NOT EXISTS actions TEXT NOT NULL DEFAULT '[]'",
    )

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            for statement in self.DDL:
                cur.execute(statement)
        conn.commit()

    def open(self, shipment_id: str, case_id: str) -> OpenCase | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO open_cases (shipment_id, case_id) VALUES (%s, %s) "
                "ON CONFLICT (shipment_id) DO NOTHING",
                (shipment_id, case_id),
            )
        self._conn.commit()
        current = self.get(shipment_id)
        return None if current is None or current.case_id == case_id else current

    def close(self, case_id: str) -> None:
        self._execute("DELETE FROM open_cases WHERE case_id = %s", (case_id,))

    def hold(self, case_id: str, reason: str, actions: tuple[Action, ...] = ()) -> None:
        self._execute(
            "UPDATE open_cases SET status = 'held', reason = %s, actions = %s WHERE case_id = %s",
            (reason, json.dumps([a.model_dump(mode="json") for a in actions]), case_id),
        )

    def release(self, case_id: str) -> bool:
        return (
            self._execute(
                "DELETE FROM open_cases WHERE case_id = %s AND status = 'held'", (case_id,)
            )
            == 1
        )

    _COLUMNS = "shipment_id, case_id, status, reason, opened_at, actions"

    def get(self, shipment_id: str) -> OpenCase | None:
        rows = self._query(
            f"SELECT {self._COLUMNS} FROM open_cases WHERE shipment_id = %s", (shipment_id,)
        )
        return self._case(rows[0]) if rows else None

    def list_held(self) -> list[OpenCase]:
        rows = self._query(
            f"SELECT {self._COLUMNS} FROM open_cases WHERE status = 'held' ORDER BY opened_at"
        )
        return [self._case(row) for row in rows]

    def _query(self, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        # End the read transaction. ops-api mostly reads, and an idle open transaction
        # holds a lock on open_cases that blocks other services' startup DDL.
        self._conn.commit()
        return rows

    @staticmethod
    def _case(row: tuple[object, ...]) -> OpenCase:
        status: Status = "held" if row[2] == "held" else "open"
        reason = str(row[3]) if row[3] else None
        opened_at = row[4] if isinstance(row[4], datetime) else None
        actions = tuple(Action.model_validate(a) for a in json.loads(str(row[5])))
        return OpenCase(str(row[0]), str(row[1]), status, reason, opened_at, actions)

    def _execute(self, sql: str, params: tuple[object, ...]) -> int:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            count = cur.rowcount
        self._conn.commit()
        return count
