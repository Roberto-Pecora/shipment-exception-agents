"""Fix for review finding 6: a claim is filed with the carrier, not just recorded."""

from __future__ import annotations

import json

import httpx

from shipex.agent.carrier import CarrierError, HttpCarrierClient, InMemoryCarrier
from shipex.agent.llm import ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.contracts.events import Envelope, ExceptionRouted, StatusCode, Topic
from shipex.pipeline import InProcessPipeline
from tests.conftest import make_case, make_event

SID = "SHP-1"


def test_http_client_files_claims_with_an_idempotency_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/claims"
        assert request.headers["Idempotency-Key"] == "case-1:claim"
        assert json.loads(request.content) == {"shipment_id": SID, "amount_gbp": 120.0}
        return httpx.Response(
            201,
            json={
                "claim_id": "clm-0001",
                "shipment_id": SID,
                "amount_gbp": 120.0,
                "status": "submitted",
            },
        )

    client = HttpCarrierClient(
        "http://gw.test",
        client=httpx.Client(base_url="http://gw.test", transport=httpx.MockTransport(handler)),
    )
    assert client.open_claim(SID, 120.0, "case-1:claim").claim_id == "clm-0001"


def test_agent_claim_is_filed_and_referenced(carrier: InMemoryCarrier) -> None:
    llm = ScriptedChat(
        [
            ScriptedTurn(
                tool_calls=(("open_damage_claim", {"shipment_id": SID, "amount_gbp": 300}),)
            ),
            ScriptedTurn(
                tool_calls=(
                    (
                        "resolve_case",
                        {
                            "resolution_code": "CLAIM_OPENED",
                            "customer_message": "We filed a damage claim with the carrier.",
                        },
                    ),
                )
            ),
        ]
    )
    case = make_case(make_event(status_code=StatusCode.DAMAGED, delay_hours=None))
    outcome = ShipmentExceptionAgent(llm, carrier).run(case)
    assert outcome.actions[0].external_ref == "clm-0001"
    assert carrier.side_effects == [("claim", "case-1:claim")]


def low_value_damage() -> object:
    return make_event(status_code=StatusCode.DAMAGED, declared_value_gbp=120.0, delay_hours=None)


def resolution(p: InProcessPipeline) -> dict[str, object]:
    [record] = p.bus.records(Topic.RESOLUTIONS)
    return Envelope.from_bytes(record.value).payload


def test_playbook_auto_claim_is_filed_once(carrier: InMemoryCarrier) -> None:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier))
    p.submit(low_value_damage())  # type: ignore[arg-type]
    p.submit(low_value_damage())  # type: ignore[arg-type]
    p.drain()
    [claim] = [s for s in carrier.side_effects if s[0] == "claim"]
    actions = resolution(p)["actions"]
    assert isinstance(actions, list) and actions[0]["external_ref"] == "clm-0001"
    assert claim[1].endswith(":claim")


def test_playbook_retries_a_transient_claim_failure(carrier: InMemoryCarrier) -> None:
    carrier.fail_next("open_claim", CarrierError("503", transient=True, status=503))
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier))
    p.submit(low_value_damage())  # type: ignore[arg-type]
    p.drain()
    assert [n.channel for n in p.outbox.list()] == ["customer"]
    assert len([s for s in carrier.side_effects if s[0] == "claim"]) == 1


def test_playbook_escalates_when_the_claim_is_refused() -> None:
    carrier = InMemoryCarrier({})  # unknown shipment: the gateway answers 404
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier))
    p.submit(low_value_damage())  # type: ignore[arg-type]
    p.drain()
    [note] = p.outbox.list()
    assert note.channel == "ops" and "claim could not be filed" in note.body
    [record] = p.bus.records(Topic.ESCALATIONS)
    assert Envelope.from_bytes(record.value).payload["source"] == "playbook"


def test_routed_case_id_is_the_claim_key(carrier: InMemoryCarrier) -> None:
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat([]), carrier))
    env = p.submit(low_value_damage())  # type: ignore[arg-type]
    p.drain()
    [record] = p.bus.records(Topic.PLAYBOOK_QUEUE)
    case_id = Envelope.from_bytes(record.value).parse(ExceptionRouted).case_id
    assert case_id == env.event_id
    assert ("claim", f"{case_id}:claim") in carrier.side_effects
