"""Fix for review finding 3: at most one automated case per shipment."""

from __future__ import annotations

from shipex.agent.carrier import InMemoryCarrier
from shipex.agent.llm import ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.contracts.events import StatusCode
from shipex.pipeline import InProcessPipeline
from shipex.store.cases import InMemoryCaseRegistry
from tests.conftest import make_event

SID = "SHP-1"
ESCALATE = [
    ScriptedTurn(tool_calls=(("get_tracking", {"shipment_id": SID}),)),
    ScriptedTurn(tool_calls=(("escalate_to_human", {"reason": "needs a specialist"}),)),
]


def test_registry_semantics() -> None:
    cases = InMemoryCaseRegistry()
    assert cases.open("S", "c1") is None
    assert cases.open("S", "c1") is None  # redelivery of the same case
    assert cases.open("S", "c2") == "c1"  # a second case is refused
    cases.close("c1")
    assert cases.open("S", "c2") is None


def test_second_exception_while_a_case_is_open_goes_to_a_human(
    carrier: InMemoryCarrier,
) -> None:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(ESCALATE), carrier))
    p.submit(make_event(carrier_event_id="evt-1", delay_hours=72.0))  # agent case
    p.submit(make_event(carrier_event_id="evt-2", status_code=StatusCode.DAMAGED))
    p.drain()

    bodies = sorted(n.body for n in p.outbox.list())
    assert len(bodies) == 2
    assert any("concurrent_case" in b for b in bodies)
    assert carrier.side_effects == []
    assert p.cases._open == {}  # both cases closed once their final events were written


def test_a_new_exception_after_the_case_closes_is_handled_normally(
    carrier: InMemoryCarrier,
) -> None:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(ESCALATE), carrier))
    p.submit(make_event(carrier_event_id="evt-1", delay_hours=72.0))
    p.drain()
    p.submit(make_event(carrier_event_id="evt-2", status_code=StatusCode.ADDRESS_ISSUE))
    p.drain()

    channels = [n.channel for n in p.outbox.list()]
    assert channels == ["ops", "customer"]  # the address playbook ran; no concurrency block
