"""Transport-neutral bus interfaces.

Services depend on these Protocols, never on a client library: Kafka in production
(`bus.kafka`), an in-memory log in tests and offline evals (`bus.memory`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Record:
    topic: str
    key: str
    value: bytes
    headers: Mapping[str, str] = field(default_factory=dict)
    partition: int = 0
    offset: int = -1


class Publisher(Protocol):
    def publish(
        self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None
    ) -> None: ...

    def flush(self, timeout: float = 10.0) -> None: ...


class Subscriber(Protocol):
    def poll(self, timeout: float) -> Record | None: ...

    def commit(self, record: Record) -> None: ...

    def close(self) -> None: ...
