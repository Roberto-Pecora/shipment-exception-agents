"""Delivery semantics of ConsumerRunner on the in-memory bus."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from shipex.bus.consumer import (
    ConsumerRunner,
    Handler,
    Outgoing,
    PermanentError,
    RetryPolicy,
    TransientError,
)
from shipex.bus.memory import InMemoryBus
from shipex.contracts.events import CaseEscalated, Envelope, ShipmentEvent
from shipex.store.idempotency import InMemoryIdempotencyStore
from tests.conftest import make_event

IN, OUT = "in.v1", "out.v1"


def echo(env: Envelope) -> Sequence[Outgoing]:
    event = env.parse(ShipmentEvent)
    esc = CaseEscalated(
        case_id=env.event_id, shipment_id=event.shipment_id, source="router", reason="echo"
    )
    return [Outgoing(OUT, env.derive(esc, salt="echo"))]


def runner(
    bus: InMemoryBus,
    handler: Handler,
    idem: InMemoryIdempotencyStore | None = None,
    attempts: int = 3,
) -> ConsumerRunner:
    return ConsumerRunner(
        "svc",
        bus.subscriber("svc", [IN]),
        bus,
        handler,
        idem or InMemoryIdempotencyStore(),
        RetryPolicy(max_attempts=attempts),
        sleep=lambda _s: None,
    )


def publish(bus: InMemoryBus, carrier_event_id: str = "evt-1") -> Envelope:
    env = Envelope.for_shipment_event(make_event(carrier_event_id=carrier_event_id))
    bus.publish(IN, env.key, env.to_bytes())
    return env


def test_processes_publishes_then_commits() -> None:
    bus = InMemoryBus()
    publish(bus)
    assert runner(bus, echo).run_once() == "processed"
    assert len(bus.records(OUT)) == 1
    assert bus.committed("svc", IN) == 1


def test_duplicate_event_ids_are_skipped() -> None:
    bus = InMemoryBus()
    publish(bus)
    publish(bus)  # carrier re-sent the same event
    r = runner(bus, echo)
    assert [r.run_once(), r.run_once()] == ["processed", "duplicate"]
    assert len(bus.records(OUT)) == 1


def test_transient_failure_is_retried_then_succeeds() -> None:
    bus = InMemoryBus()
    publish(bus)
    calls = {"n": 0}

    def flaky(env: Envelope) -> Sequence[Outgoing]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransientError("carrier-gateway 503")
        return echo(env)

    assert runner(bus, flaky).run_once() == "processed"
    assert calls["n"] == 3
    assert bus.records("in.v1.dlq") == []


def test_exhausted_retries_go_to_dlq_with_context_and_commit() -> None:
    bus = InMemoryBus()
    env = publish(bus)

    def down(_env: Envelope) -> Sequence[Outgoing]:
        raise TransientError("still down")

    assert runner(bus, down, attempts=2).run_once() == "dead_lettered"
    [dead] = bus.records("in.v1.dlq")
    assert dead.value == env.to_bytes()
    assert dead.headers["x-attempts"] == "2"
    assert "still down" in dead.headers["x-error"]
    assert bus.committed("svc", IN) == 1  # partition is not blocked


def test_permanent_error_skips_retries() -> None:
    bus = InMemoryBus()
    publish(bus)
    calls = {"n": 0}

    def bad(_env: Envelope) -> Sequence[Outgoing]:
        calls["n"] += 1
        raise PermanentError("contract violation")

    assert runner(bus, bad).run_once() == "dead_lettered"
    assert calls["n"] == 1


def test_poison_record_is_dead_lettered() -> None:
    bus = InMemoryBus()
    bus.publish(IN, "k", b"not json")
    assert runner(bus, echo).run_once() == "dead_lettered"
    assert bus.records("in.v1.dlq")[0].headers["x-error"].startswith("poison record")


def test_crash_after_publish_redelivers_with_identical_output_ids() -> None:
    """At-least-once: a crash before commit replays the input; outputs keep their ids."""
    bus = InMemoryBus()
    publish(bus)

    class CrashOnMark(InMemoryIdempotencyStore):
        def mark(self, consumer: str, event_id: str) -> None:
            raise RuntimeError("process killed")

    with pytest.raises(RuntimeError):
        runner(bus, echo, CrashOnMark()).run_once()
    assert bus.committed("svc", IN) == 0

    assert runner(bus, echo).run_once() == "processed"  # restarted consumer
    ids = [Envelope.from_bytes(r.value).event_id for r in bus.records(OUT)]
    assert len(ids) == 2 and ids[0] == ids[1]  # downstream dedupes the replay
