"""The whole service graph in-process: routing, playbooks, agent, notifier, idempotency."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from shipex.agent.carrier import InMemoryCarrier
from shipex.agent.llm import ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.bus.memory import InMemoryBus
from shipex.contracts.events import Envelope, ExceptionRouted, StatusCode, Topic
from shipex.pipeline import InProcessPipeline
from shipex.services.ingest_api import create_app
from shipex.store.idempotency import InMemoryIdempotencyStore
from tests.conftest import make_event

SID = "SHP-1"
REBOOK_SCRIPT = [
    ScriptedTurn(tool_calls=(("get_rebooking_options", {"shipment_id": SID}),)),
    ScriptedTurn(tool_calls=(("rebook_shipment", {"shipment_id": SID, "option_id": "opt-air"}),)),
    ScriptedTurn(
        tool_calls=(
            (
                "resolve_case",
                {
                    "resolution_code": "REBOOKED",
                    "customer_message": "Your shipment has been moved to air express.",
                },
            ),
        )
    ),
]


def pipeline(
    carrier: InMemoryCarrier, script: list[ScriptedTurn] | None = None
) -> InProcessPipeline:
    return InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(script or []), carrier))


def channels(p: InProcessPipeline) -> list[str]:
    return [n.channel for n in p.outbox.list()]


def test_routine_scan_produces_nothing(carrier: InMemoryCarrier) -> None:
    p = pipeline(carrier)
    p.submit(make_event(status_code=StatusCode.IN_TRANSIT))
    p.drain()
    assert p.outbox.list() == []


def test_playbook_case_never_calls_the_llm(carrier: InMemoryCarrier) -> None:
    p = pipeline(carrier)  # empty script: any LLM call would fail and escalate
    p.submit(make_event())
    p.drain()
    [note] = p.outbox.list()
    assert note.channel == "customer" and "running about 6 hours late" in note.body


def test_high_value_goes_to_ops(carrier: InMemoryCarrier) -> None:
    p = pipeline(carrier)
    p.submit(make_event(declared_value_gbp=50_000.0))
    p.drain()
    [note] = p.outbox.list()
    assert note.channel == "ops" and "high_value" in note.body


def test_agent_case_end_to_end(carrier: InMemoryCarrier) -> None:
    p = pipeline(carrier, list(REBOOK_SCRIPT))
    p.submit(make_event(delay_hours=72.0))
    p.drain()
    [note] = p.outbox.list()
    assert note.channel == "customer" and "air express" in note.body
    [resolution] = p.bus.records(Topic.RESOLUTIONS)
    assert Envelope.from_bytes(resolution.value).payload["run"]["steps"] == 3


def test_resubmitted_event_is_processed_once(carrier: InMemoryCarrier) -> None:
    p = pipeline(carrier, list(REBOOK_SCRIPT))
    p.submit(make_event(delay_hours=72.0))
    p.submit(make_event(delay_hours=72.0))
    p.drain()
    assert len(p.outbox.list()) == 1
    assert len(carrier.side_effects) == 1


CLAIM_INSTEAD = [
    ScriptedTurn(tool_calls=(("open_damage_claim", {"shipment_id": SID, "amount_gbp": 300}),)),
    ScriptedTurn(
        tool_calls=(
            (
                "resolve_case",
                {"resolution_code": "CLAIM_OPENED", "customer_message": "We opened a claim."},
            ),
        )
    ),
]


def test_agent_redelivery_reuses_the_first_outcome(carrier: InMemoryCarrier) -> None:
    """Worker 'crashes' after the run but before committing. The redelivered case must not
    re-run the agent: here a second run would choose a *different* action (a claim)."""
    llm = ScriptedChat(list(REBOOK_SCRIPT) + CLAIM_INSTEAD)
    p = InProcessPipeline(ShipmentExceptionAgent(llm, carrier))
    p.submit(make_event(delay_hours=72.0))
    router, _playbook, agent, _notifier = p.runners
    router.run_once(0)
    record = p.bus.subscriber("probe", [Topic.AGENT_QUEUE]).poll(0)
    assert record is not None
    envelope = Envelope.from_bytes(record.value)
    agent.process(record)
    p.idempotency._seen.discard(("agent", envelope.event_id))
    agent.process(record)  # redelivery after a crash before the ledger write
    p.drain()

    case_id = envelope.parse(ExceptionRouted).case_id
    assert carrier.side_effects == [("rebook", f"{case_id}:rebook")]
    assert len(llm.seen) == len(REBOOK_SCRIPT)  # the model was not called again
    [note] = p.outbox.list()
    assert "air express" in note.body


def test_ingest_api_dedupes_and_validates() -> None:
    bus = InMemoryBus()
    api = TestClient(create_app(bus, InMemoryIdempotencyStore()))
    body: dict[str, Any] = make_event().model_dump(mode="json")

    first = api.post("/v1/shipment-events", json=body)
    second = api.post("/v1/shipment-events", json=body)
    assert first.status_code == second.status_code == 202
    assert (first.json()["duplicate"], second.json()["duplicate"]) == (False, True)
    assert len(bus.records(Topic.SHIPMENT_EVENTS)) == 1

    assert api.post("/v1/shipment-events", json={**body, "status_code": "NOPE"}).status_code == 422
    assert api.get("/healthz").json() == {"status": "ok"}
