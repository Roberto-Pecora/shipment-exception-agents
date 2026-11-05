"""A worker killed *during* an agent run must not let a second run act differently."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from shipex.agent.carrier import CarrierError, InMemoryCarrier
from shipex.agent.llm import ChatReply, Message, ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.contracts.events import CaseEscalated, Envelope, StatusCode
from shipex.handlers import agent_handler
from shipex.store.journal import InMemoryActionJournal
from shipex.store.outcomes import InMemoryOutcomeStore
from tests.conftest import make_case

SID = "SHP-1"
REBOOK_THEN_CRASH = [
    ScriptedTurn(tool_calls=(("get_rebooking_options", {"shipment_id": SID}),)),
    ScriptedTurn(tool_calls=(("rebook_shipment", {"shipment_id": SID, "option_id": "opt-air"}),)),
]


class Killed(BaseException):
    """Stands in for SIGKILL: not an Exception, so nothing in the loop can catch it."""


class CrashAfterScript(ScriptedChat):
    def chat(
        self, messages: Sequence[Message], tools: Sequence[Message], timeout_s: float | None = None
    ) -> ChatReply:
        if not self._turns:
            raise Killed()
        return super().chat(messages, tools, timeout_s)


def routed_envelope() -> Envelope:
    case = make_case()
    return Envelope.for_shipment_event(case.shipment).derive(case, salt="route")


def test_redelivery_after_a_mid_run_kill_escalates_instead_of_rerunning(
    carrier: InMemoryCarrier,
) -> None:
    journal = InMemoryActionJournal()
    outcomes = InMemoryOutcomeStore()
    env = routed_envelope()

    first = ShipmentExceptionAgent(CrashAfterScript(REBOOK_THEN_CRASH), carrier, journal=journal)
    with pytest.raises(Killed):
        agent_handler(first, outcomes)(env)  # rebooked, then the worker died

    # The replacement worker's model would open a claim; it must never be asked.
    claim = ScriptedChat(
        [ScriptedTurn(tool_calls=(("open_damage_claim", {"shipment_id": SID, "amount_gbp": 50}),))]
    )
    second = ShipmentExceptionAgent(claim, carrier, journal=journal)
    [out] = agent_handler(second, outcomes)(env)

    event = out.envelope.parse(CaseEscalated)
    assert "interrupted" in event.reason and "rebook (rb-0001)" in event.reason
    assert [(a.kind, a.external_ref) for a in event.actions] == [("rebook", "rb-0001")]
    assert claim.seen == []  # no second agent run
    assert [kind for kind, _ in carrier.side_effects] == ["rebook"]


def test_intent_without_confirmation_is_reported_as_unknown(carrier: InMemoryCarrier) -> None:
    """Killed after journalling the intent but before the carrier answered."""
    journal = InMemoryActionJournal()
    case = make_case(make_case().shipment.model_copy(update={"status_code": StatusCode.DAMAGED}))
    journal.record_intent(case.case_id, f"{case.case_id}:claim", "claim", {"amount_gbp": 80})
    env = Envelope.for_shipment_event(case.shipment).derive(case, salt="route")

    agent = ShipmentExceptionAgent(ScriptedChat([]), carrier, journal=journal)
    [out] = agent_handler(agent, InMemoryOutcomeStore())(env)
    event = out.envelope.parse(CaseEscalated)
    assert "claim (outcome unknown)" in event.reason
    assert event.actions == ()  # unconfirmed actions are not claimed as done


def test_side_effects_are_journalled_before_the_carrier_call(carrier: InMemoryCarrier) -> None:
    journal = InMemoryActionJournal()
    carrier.fail_next("rebook", CarrierError("503", transient=True, status=503))
    llm = ScriptedChat(
        REBOOK_THEN_CRASH
        + [ScriptedTurn(tool_calls=(("escalate_to_human", {"reason": "carrier down"}),))]
    )
    ShipmentExceptionAgent(llm, carrier, journal=journal).run(make_case())
    [entry] = journal.entries("case-1")
    assert entry.kind == "rebook" and entry.external_ref is None  # intent logged, call failed


def test_a_finished_run_is_journalled_and_confirmed(carrier: InMemoryCarrier) -> None:
    journal = InMemoryActionJournal()
    llm = ScriptedChat(
        REBOOK_THEN_CRASH
        + [
            ScriptedTurn(
                tool_calls=(
                    (
                        "resolve_case",
                        {
                            "resolution_code": "REBOOKED",
                            "customer_message": "Moved to air express.",
                        },
                    ),
                )
            )
        ]
    )
    ShipmentExceptionAgent(llm, carrier, journal=journal).run(make_case())
    assert [(e.kind, e.external_ref) for e in journal.entries("case-1")] == [("rebook", "rb-0001")]
