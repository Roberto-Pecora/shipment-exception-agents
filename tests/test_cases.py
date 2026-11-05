"""At most one case per shipment, and escalated cases stay with ops until released."""

from __future__ import annotations

from fastapi.testclient import TestClient

from shipex.agent.carrier import InMemoryCarrier
from shipex.agent.llm import ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.contracts.events import StatusCode
from shipex.pipeline import InProcessPipeline
from shipex.services.ops_api import create_app
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
    existing = cases.open("S", "c2")  # a second case is refused
    assert existing is not None and existing.case_id == "c1" and existing.status == "open"
    assert cases.release("c1") is False  # only held cases can be released
    cases.hold("c1", "needs a specialist")
    assert cases.open("S", "c2") is not None  # still blocked while held
    assert cases.release("c1") is True
    assert cases.open("S", "c2") is None
    cases.hold("unknown", "x")  # holding an unregistered case is a no-op
    assert cases.get("S") is not None and cases.get("S").case_id == "c2"  # type: ignore[union-attr]


def test_second_exception_while_a_case_is_open_goes_to_a_human(
    carrier: InMemoryCarrier,
) -> None:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(ESCALATE), carrier))
    p.submit(make_event(carrier_event_id="evt-1", delay_hours=72.0))  # agent case
    p.submit(make_event(carrier_event_id="evt-2", status_code=StatusCode.DAMAGED))
    p.drain()

    bodies = sorted(n.body for n in p.outbox.list())
    assert len(bodies) == 2
    assert any("concurrent_case" in b and "in progress" in b for b in bodies)
    assert carrier.side_effects == []


def test_resolved_cases_close(carrier: InMemoryCarrier) -> None:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier))
    p.submit(make_event(carrier_event_id="evt-1"))  # 6h delay: re-ETA playbook resolves it
    p.drain()
    assert p.cases.get(SID) is None


def test_escalated_case_is_held_until_ops_releases_it(carrier: InMemoryCarrier) -> None:
    """A person working an escalated shipment must not have automation act alongside them."""
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(ESCALATE), carrier))
    ops = TestClient(create_app(p.cases))
    p.submit(make_event(carrier_event_id="evt-1", delay_hours=72.0))
    p.drain()

    held = ops.get(f"/v1/shipments/{SID}/case").json()
    assert held["status"] == "held" and "needs a specialist" in held["reason"]

    # A later exception while ops holds the case: routed to humans, playbook not run.
    p.submit(make_event(carrier_event_id="evt-2", status_code=StatusCode.ADDRESS_ISSUE))
    p.drain()
    assert [n.channel for n in p.outbox.list()] == ["ops", "ops"]
    assert "held by ops" in p.outbox.list()[1].body

    # Ops releases; the next exception is automated again.
    assert ops.post(f"/v1/cases/{held['case_id']}/release").status_code == 204
    assert ops.get(f"/v1/shipments/{SID}/case").status_code == 404
    p.submit(make_event(carrier_event_id="evt-3", status_code=StatusCode.ADDRESS_ISSUE))
    p.drain()
    assert [n.channel for n in p.outbox.list()] == ["ops", "ops", "customer"]


def test_release_requires_a_held_case() -> None:
    cases = InMemoryCaseRegistry()
    cases.open("S", "c1")
    ops = TestClient(create_app(cases))
    assert ops.post("/v1/cases/c1/release").status_code == 404  # in progress, not held
    assert ops.post("/v1/cases/nope/release").status_code == 404
