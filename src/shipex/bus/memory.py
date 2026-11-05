"""In-memory append-only log with consumer-group offsets.

Mirrors the Kafka semantics the services rely on: per-group committed offsets, and
redelivery of anything uncommitted when a consumer restarts. That is enough to test
at-least-once delivery, idempotency and DLQ routing without a broker.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence

from shipex.bus.base import Record


class InMemoryBus:
    def __init__(self) -> None:
        self._log: dict[str, list[Record]] = defaultdict(list)
        self._committed: dict[tuple[str, str], int] = {}

    def publish(
        self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None
    ) -> None:
        log = self._log[topic]
        log.append(Record(topic, key, value, dict(headers or {}), 0, len(log)))

    def flush(self, timeout: float = 10.0) -> None:
        return None

    def subscriber(self, group: str, topics: Sequence[str]) -> InMemorySubscriber:
        return InMemorySubscriber(self, group, topics)

    def records(self, topic: str) -> list[Record]:
        return list(self._log.get(topic, []))

    def committed(self, group: str, topic: str) -> int:
        return self._committed.get((group, topic), 0)

    def _commit(self, group: str, topic: str, next_offset: int) -> None:
        key = (group, topic)
        self._committed[key] = max(self._committed.get(key, 0), next_offset)


class InMemorySubscriber:
    """Starts at the group's committed offset, like a restarted Kafka consumer."""

    def __init__(self, bus: InMemoryBus, group: str, topics: Sequence[str]) -> None:
        self._bus = bus
        self._group = group
        self._topics = list(topics)
        self._position = {t: bus.committed(group, t) for t in self._topics}

    def poll(self, timeout: float = 0.0) -> Record | None:
        for topic in self._topics:
            log = self._bus._log.get(topic, [])
            pos = self._position[topic]
            if pos < len(log):
                self._position[topic] = pos + 1
                return log[pos]
        return None

    def commit(self, record: Record) -> None:
        self._bus._commit(self._group, record.topic, record.offset + 1)

    def close(self) -> None:
        return None
