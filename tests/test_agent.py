"""Agent loop, guardrails and budgets, with a scripted model and an in-memory carrier."""

from __future__ import annotations

import json
from typing import Any

from shipex.agent.carrier import CarrierError, InMemoryCarrier
from shipex.agent.llm import ScriptedChat, ScriptedTurn, Usage
from shipex.agent.loop import AgentBudget, ShipmentExceptionAgent
from shipex.contracts.events import CaseEscalated, CaseResolved, ResolutionCode, StatusCode
from tests.conftest import make_case, make_event

SID = "SHP-1"


def turn(*calls: tuple[str, dict[str, Any] | str]) -> ScriptedTurn:
    return ScriptedTurn(tool_calls=calls)


def resolve(
    code: str, message: str = "Your shipment was moved to a faster service."
) -> ScriptedTurn:
    return turn(("resolve_case", {"resolution_code": code, "customer_message": message}))


def escalate(reason: str = "needs a specialist") -> ScriptedTurn:
    return turn(("escalate_to_human", {"reason": reason}))


def run(turns: list[ScriptedTurn], carrier: InMemoryCarrier, **budget: Any) -> Any:
    llm = ScriptedChat(turns)
    agent = ShipmentExceptionAgent(llm, carrier, AgentBudget(**budget) if budget else None)
    case = make_case(make_event(status_code=StatusCode.DELAYED, delay_hours=72.0))
    return agent.run(case), llm


def test_happy_path_rebook_then_resolve(carrier: InMemoryCarrier) -> None:
    outcome, _ = run(
        [
            turn(("get_tracking", {"shipment_id": SID})),
            turn(("get_rebooking_options", {"shipment_id": SID})),
            turn(("rebook_shipment", {"shipment_id": SID, "option_id": "opt-air"})),
            resolve("REBOOKED"),
        ],
        carrier,
    )
    assert outcome.status == "resolved"
    assert outcome.resolution_code is ResolutionCode.REBOOKED
    assert [a.kind for a in outcome.actions] == ["rebook"]
    assert outcome.actions[0].external_ref == "rb-0001"
    assert outcome.stats.steps == 4 and outcome.stats.guardrail_blocks == 0
    assert carrier.side_effects == [("rebook", "case-1:rebook")]
    assert isinstance(outcome.to_event(make_case()), CaseResolved)


def test_rebook_over_cost_cap_is_blocked_and_agent_escalates(carrier: InMemoryCarrier) -> None:
    outcome, llm = run(
        [
            turn(("get_rebooking_options", {"shipment_id": SID})),
            turn(("rebook_shipment", {"shipment_id": SID, "option_id": "opt-charter"})),
            escalate("only option exceeds the cost cap"),
        ],
        carrier,
    )
    assert outcome.status == "escalated"
    assert outcome.stats.guardrail_blocks == 1
    assert carrier.side_effects == []  # the blocked call never reached the carrier
    blocked = json.loads(llm.seen[-1][-1]["content"])
    assert "exceeds the £150.00 cap" in blocked["blocked"]


def test_hallucinated_option_id_is_blocked(carrier: InMemoryCarrier) -> None:
    outcome, _ = run(
        [turn(("rebook_shipment", {"shipment_id": SID, "option_id": "opt-made-up"})), escalate()],
        carrier,
    )
    assert outcome.transcript[0].outcome == "blocked"
    assert "was not offered" in outcome.transcript[0].detail


def test_cross_shipment_action_is_blocked(carrier: InMemoryCarrier) -> None:
    """Prompt injection in carrier text can't redirect actions to another shipment."""
    outcome, _ = run(
        [turn(("get_tracking", {"shipment_id": "SHP-OTHER"})), escalate()],
        carrier,
    )
    assert outcome.transcript[0].outcome == "blocked"


def test_resolution_must_be_backed_by_an_action(carrier: InMemoryCarrier) -> None:
    outcome, _ = run([resolve("REBOOKED"), escalate()], carrier)
    assert outcome.status == "escalated"
    assert "requires a successful rebook" in outcome.transcript[0].detail


def test_customer_message_cannot_promise_refunds(carrier: InMemoryCarrier) -> None:
    outcome, _ = run(
        [
            resolve("CUSTOMER_NOTIFIED", "Sorry for the delay, you will get a full refund."),
            escalate(),
        ],
        carrier,
    )
    assert outcome.transcript[0].outcome == "blocked"


def test_invalid_arguments_are_returned_to_the_model(carrier: InMemoryCarrier) -> None:
    outcome, llm = run(
        [
            turn(("get_tracking", "{not json")),
            turn(("open_damage_claim", {"shipment_id": SID, "amount_gbp": -5})),
            turn(("get_tracking", {"shipment_id": SID})),
            escalate(),
        ],
        carrier,
    )
    assert outcome.stats.invalid_tool_calls == 2
    assert outcome.status == "escalated"
    assert "invalid arguments" in llm.seen[1][-1]["content"]


def test_too_many_invalid_calls_escalates(carrier: InMemoryCarrier) -> None:
    outcome, _ = run([turn(("no_such_tool", {}))] * 5, carrier, max_invalid_calls=2)
    assert outcome.reason.startswith("protocol:")


def test_text_reply_is_nudged_back_to_tools(carrier: InMemoryCarrier) -> None:
    outcome, llm = run([ScriptedTurn(content="I think we should rebook."), escalate()], carrier)
    assert outcome.status == "escalated"
    assert "without a tool call" in llm.seen[1][-1]["content"]


def test_llm_outage_degrades_to_escalation(carrier: InMemoryCarrier) -> None:
    outcome, _ = run([ScriptedTurn(error="503 from provider")], carrier)
    assert outcome.status == "escalated"
    assert outcome.reason.startswith("llm_unavailable")


def test_step_budget(carrier: InMemoryCarrier) -> None:
    outcome, _ = run([turn(("get_tracking", {"shipment_id": SID}))] * 10, carrier, max_steps=3)
    assert outcome.reason.startswith("budget:steps")
    assert outcome.stats.steps == 3


def test_cost_budget(carrier: InMemoryCarrier) -> None:
    llm = ScriptedChat(
        [turn(("get_tracking", {"shipment_id": SID}))] * 10,
        usage=Usage(prompt_tokens=1000, completion_tokens=100, cost_usd=0.02),
    )
    agent = ShipmentExceptionAgent(llm, carrier, AgentBudget(max_cost_usd=0.05))
    outcome = agent.run(make_case())
    assert outcome.reason.startswith("budget:cost")
    assert outcome.stats.steps == 3


def test_time_budget(carrier: InMemoryCarrier) -> None:
    ticks = iter([0.0, 0.0, 50.0, 100.0, 100.0, 100.0])
    agent = ShipmentExceptionAgent(
        ScriptedChat([turn(("get_tracking", {"shipment_id": SID}))] * 5),
        carrier,
        AgentBudget(max_wall_s=60.0),
        clock=lambda: next(ticks),
    )
    assert agent.run(make_case()).reason.startswith("budget:time")


def test_carrier_errors_are_surfaced_not_raised(carrier: InMemoryCarrier) -> None:
    carrier.fail_next("get_tracking", CarrierError("503 Service Unavailable", transient=True))
    outcome, llm = run(
        [turn(("get_tracking", {"shipment_id": SID})), escalate("carrier down")], carrier
    )
    result = json.loads(llm.seen[1][-1]["content"])
    assert result["kind"].startswith("temporary")
    assert outcome.status == "escalated"


def test_customs_hold_requests_documents(carrier: InMemoryCarrier) -> None:
    carrier.fixtures[SID] = carrier.fixtures[SID].model_copy(
        update={
            "tracking": carrier.fixtures[SID].tracking.model_copy(
                update={"missing_customs_documents": ["commercial invoice"]}
            )
        }
    )
    outcome, _ = run(
        [
            turn(("get_tracking", {"shipment_id": SID})),
            turn(
                (
                    "request_customs_documents",
                    {"shipment_id": SID, "documents": ["commercial invoice"]},
                )
            ),
            resolve("CUSTOMS_DOCS_REQUESTED", "Customs need the commercial invoice to release it."),
        ],
        carrier,
    )
    assert outcome.resolution_code is ResolutionCode.CUSTOMS_DOCS_REQUESTED
    assert outcome.actions[0].external_ref == "cdr-0001"


def test_escalation_keeps_actions_already_taken(carrier: InMemoryCarrier) -> None:
    outcome, _ = run(
        [
            turn(("request_customs_documents", {"shipment_id": SID, "documents": ["EORI"]})),
            escalate("shipper unreachable"),
        ],
        carrier,
    )
    event = outcome.to_event(make_case())
    assert isinstance(event, CaseEscalated)
    assert [a.kind for a in event.actions] == ["customs_docs_request"]
