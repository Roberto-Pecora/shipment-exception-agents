"""Notification outbox: the notifier's sink, keyed by notification id (idempotent insert).

A real deployment would relay these rows to email/SMS/ticketing; here the table *is*
the observable end state that the smoke test and evals assert on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import psycopg

Channel = Literal["customer", "ops"]


@dataclass(frozen=True)
class Notification:
    notification_id: str
    case_id: str
    shipment_id: str
    channel: Channel
    body: str


class Outbox(Protocol):
    def add(self, notification: Notification) -> bool: ...

    def list(self) -> list[Notification]: ...


class InMemoryOutbox:
    def __init__(self) -> None:
        self._rows: dict[str, Notification] = {}

    def add(self, notification: Notification) -> bool:
        if notification.notification_id in self._rows:
            return False
        self._rows[notification.notification_id] = notification
        return True

    def list(self) -> list[Notification]:
        return list(self._rows.values())


class PostgresOutbox:
    DDL = """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        notification_id TEXT PRIMARY KEY,
        case_id         TEXT        NOT NULL,
        shipment_id     TEXT        NOT NULL,
        channel         TEXT        NOT NULL,
        body            TEXT        NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute(self.DDL)
        conn.commit()

    def add(self, notification: Notification) -> bool:
        n = notification
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notification_outbox "
                "(notification_id, case_id, shipment_id, channel, body) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (notification_id) DO NOTHING",
                (n.notification_id, n.case_id, n.shipment_id, n.channel, n.body),
            )
            inserted = cur.rowcount == 1
        self._conn.commit()
        return inserted

    def list(self) -> list[Notification]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT notification_id, case_id, shipment_id, channel, body "
                "FROM notification_outbox ORDER BY created_at"
            )
            rows = cur.fetchall()
        return [
            Notification(
                str(r[0]), str(r[1]), str(r[2]), "ops" if r[3] == "ops" else "customer", str(r[4])
            )
            for r in rows
        ]
