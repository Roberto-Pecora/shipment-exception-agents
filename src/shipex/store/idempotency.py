"""Processed-event ledger: makes at-least-once delivery effectively-once per consumer."""

from __future__ import annotations

from typing import Protocol

import psycopg


class IdempotencyStore(Protocol):
    def seen(self, consumer: str, event_id: str) -> bool: ...

    def mark(self, consumer: str, event_id: str) -> None: ...


class InMemoryIdempotencyStore:
    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def seen(self, consumer: str, event_id: str) -> bool:
        return (consumer, event_id) in self._seen

    def mark(self, consumer: str, event_id: str) -> None:
        self._seen.add((consumer, event_id))


class PostgresIdempotencyStore:
    DDL = """
    CREATE TABLE IF NOT EXISTS processed_events (
        consumer     TEXT        NOT NULL,
        event_id     TEXT        NOT NULL,
        processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (consumer, event_id)
    )
    """

    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute(self.DDL)
        conn.commit()

    def seen(self, consumer: str, event_id: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM processed_events WHERE consumer = %s AND event_id = %s",
                (consumer, event_id),
            )
            return cur.fetchone() is not None

    def mark(self, consumer: str, event_id: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO processed_events (consumer, event_id) VALUES (%s, %s) "
                "ON CONFLICT (consumer, event_id) DO NOTHING",
                (consumer, event_id),
            )
        self._conn.commit()
