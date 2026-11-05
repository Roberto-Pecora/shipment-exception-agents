"""Infrastructure failure drills on the in-process service graph.

Model-level failures (LLM outage, carrier outage, malformed tool calls, budget loops)
are scenarios in scenarios.jsonl. This suite covers the delivery layer: poison
messages, a crashing worker, a flaky sink and a redelivery storm.

  python evals/failure_suite.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from shipex.agent.carrier import CarrierFixture, InMemoryCarrier, RebookingOption, TrackingInfo
from shipex.agent.llm import ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.contracts.events import ShipmentEvent, Topic
from shipex.evaluation import BASE_EVENT, NOW
from shipex.pipeline import InProcessPipeline
from shipex.store.outbox import Notification

Check = Callable[[], tuple[bool, str]]


def event(i: int, **overrides: object) -> ShipmentEvent:
    fields = dict(BASE_EVENT, carrier_event_id=f"evt-{i}", shipment_id=f"SHP-{i}")
    fields.update(overrides)
    return ShipmentEvent.model_validate(fields)


def rebook_script(sid: str) -> list[ScriptedTurn]:
    return [
        ScriptedTurn(tool_calls=(("get_rebooking_options", {"shipment_id": sid}),)),
        ScriptedTurn(
            tool_calls=(("rebook_shipment", {"shipment_id": sid, "option_id": "opt-air"}),)
        ),
        ScriptedTurn(
            tool_calls=(
                (
                    "resolve_case",
                    {
                        "resolution_code": "REBOOKED",
                        "customer_message": "Your shipment was moved to air express.",
                    },
                ),
            )
        ),
    ]


def carrier_for(*sids: str) -> InMemoryCarrier:
    option = RebookingOption(
        option_id="opt-air",
        service="air express",
        new_eta=NOW + timedelta(days=1),
        cost_delta_gbp=95.0,
    )
    return InMemoryCarrier(
        {
            sid: CarrierFixture(
                tracking=TrackingInfo(shipment_id=sid, status="DELAYED"),
                rebooking_options=[option],
            )
            for sid in sids
        }
    )


def poison_messages_are_isolated() -> tuple[bool, str]:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier_for()))
    p.bus.publish(Topic.SHIPMENT_EVENTS, "x", b"{not an envelope")
    p.submit(event(1))  # a healthy playbook case behind the poison record
    p.drain()
    dlq = p.bus.records(f"{Topic.SHIPMENT_EVENTS}.dlq")
    ok = len(dlq) == 1 and len(p.outbox.list()) == 1
    return ok, f"dlq={len(dlq)}, healthy cases notified={len(p.outbox.list())}"


def agent_worker_crash_does_not_double_book() -> tuple[bool, str]:
    sid = "SHP-2"
    carrier = carrier_for(sid)
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(rebook_script(sid) * 2), carrier))
    p.submit(event(2, delay_hours=72.0))
    router, _playbook, agent, _notifier = p.runners
    router.run_once(0)
    record = p.bus.subscriber("drill", [Topic.AGENT_QUEUE]).poll(0)
    assert record is not None

    original_mark = p.idempotency.mark

    def crash(consumer: str, event_id: str) -> None:
        raise RuntimeError("worker killed before recording progress")

    p.idempotency.mark = crash  # type: ignore[method-assign]
    try:
        agent.process(record)
    except RuntimeError:
        pass
    p.idempotency.mark = original_mark  # type: ignore[method-assign]
    p.drain()  # restarted worker redelivers the uncommitted record
    rebooks = [e for e in carrier.side_effects if e[0] == "rebook"]
    ok = len(rebooks) == 1 and len(p.outbox.list()) == 1
    return ok, f"rebooks={len(rebooks)}, customer notifications={len(p.outbox.list())}"


def flaky_sink_is_retried() -> tuple[bool, str]:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier_for()))
    real_add = p.outbox.add
    failures = {"left": 1}

    def flaky_add(n: Notification) -> bool:
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("database connection reset")
        return real_add(n)

    p.outbox.add = flaky_add  # type: ignore[method-assign]
    p.submit(event(3))
    p.drain()
    dlq = p.bus.records(f"{Topic.RESOLUTIONS}.dlq")
    ok = len(p.outbox.list()) == 1 and not dlq
    return ok, f"notified={len(p.outbox.list())} after 1 transient failure, dlq={len(dlq)}"


def dead_sink_goes_to_dlq_without_blocking() -> tuple[bool, str]:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier_for()))
    real_add = p.outbox.add

    def dead_for_shp4(n: Notification) -> bool:
        if n.shipment_id == "SHP-4":
            raise ConnectionError("outbox unavailable for this partition")
        return real_add(n)

    p.outbox.add = dead_for_shp4  # type: ignore[method-assign]
    p.submit(event(4))
    p.submit(event(5))
    p.drain()
    dlq = p.bus.records(f"{Topic.RESOLUTIONS}.dlq")
    ok = len(dlq) == 1 and [n.shipment_id for n in p.outbox.list()] == ["SHP-5"]
    return (
        ok,
        f"dlq={len(dlq)} (x-attempts={dlq[0].headers.get('x-attempts') if dlq else '-'}), "
        "later case notified",
    )


def redelivery_storm_is_absorbed() -> tuple[bool, str]:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier_for()))
    for i in range(10, 30):
        for _ in range(5):
            p.submit(event(i))
    p.drain()
    ok = len(p.outbox.list()) == 20
    return ok, f"100 deliveries of 20 events -> {len(p.outbox.list())} notifications"


CHECKS: list[tuple[str, Check]] = [
    ("poison message isolated to DLQ", poison_messages_are_isolated),
    ("agent worker crash: no double rebooking", agent_worker_crash_does_not_double_book),
    ("transient sink failure retried", flaky_sink_is_retried),
    (
        "persistent sink failure: DLQ, partition keeps flowing",
        dead_sink_goes_to_dlq_without_blocking,
    ),
    ("redelivery storm absorbed", redelivery_storm_is_absorbed),
]


def main() -> int:
    rows = ["| Drill | Result | Detail |", "|---|:-:|---|"]
    failed = 0
    for name, check in CHECKS:
        ok, detail = check()
        failed += not ok
        rows.append(f"| {name} | {'pass' if ok else 'FAIL'} | {detail} |")
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    out = Path(__file__).parent / "results" / "failure-suite.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Failure drills\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
