"""Per-service business logic as pure `Envelope -> [Outgoing]` handlers.

Handlers know nothing about Kafka, retries or idempotency — `ConsumerRunner` owns
delivery. That split is what lets the same handlers run in the deployed services and
in the in-process pipeline used by tests and evals.
"""

from __future__ import annotations

from collections.abc import Sequence

from shipex import telemetry
from shipex.agent.carrier import CarrierClient, CarrierError
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.bus.consumer import Handler, Outgoing, PermanentError, TransientError
from shipex.contracts.events import (
    Action,
    CaseEscalated,
    CaseResolved,
    Envelope,
    ExceptionRouted,
    Route,
    RoutingDecision,
    ShipmentEvent,
    Topic,
)
from shipex.playbooks import PLAYBOOKS
from shipex.routing.rules import RoutingPolicy, route
from shipex.store.cases import CaseRegistry
from shipex.store.journal import JournalEntry
from shipex.store.outbox import Notification, Outbox
from shipex.store.outcomes import OutcomeStore, StoredOutcome

SUBSCRIPTIONS: dict[str, tuple[Topic, ...]] = {
    "router": (Topic.SHIPMENT_EVENTS,),
    "playbook": (Topic.PLAYBOOK_QUEUE,),
    "agent": (Topic.AGENT_QUEUE,),
    "notifier": (Topic.RESOLUTIONS, Topic.ESCALATIONS),
}


def router_handler(
    cases: CaseRegistry, policy: RoutingPolicy | None = None, agent_only: bool = False
) -> Handler:
    """`agent_only=True` is the eval baseline: every exception goes to the LLM."""

    def handle(env: Envelope) -> Sequence[Outgoing]:
        event = env.parse(ShipmentEvent)
        with telemetry.tracer.start_as_current_span("route") as span:
            decision = route(event, policy)
            if agent_only and decision.route is not Route.IGNORE:
                decision = RoutingDecision(
                    route=Route.AGENT, rule_id="agent_only", reason="baseline: agent handles all"
                )
            if decision.route is not Route.IGNORE:
                open_case = cases.open(event.shipment_id, env.event_id)
                if open_case is not None:
                    decision = RoutingDecision(
                        route=Route.HUMAN,
                        rule_id="concurrent_case",
                        reason=(
                            f"shipment already has case {open_case.case_id} "
                            f"({'held by ops' if open_case.status == 'held' else 'in progress'}"
                            f"); a further {event.status_code.value} exception needs a human"
                        ),
                    )
            span.set_attribute("shipex.route", decision.route.value)
            span.set_attribute("shipex.rule_id", decision.rule_id)
        telemetry.ROUTES.labels(decision.route.value, decision.rule_id).inc()

        if decision.route is Route.IGNORE:
            return []
        if decision.route is Route.HUMAN:
            escalation = CaseEscalated(
                case_id=env.event_id,
                shipment_id=event.shipment_id,
                source="router",
                reason=f"{decision.rule_id}: {decision.reason}",
            )
            return [Outgoing(Topic.ESCALATIONS, env.derive(escalation, salt="route"))]
        routed = ExceptionRouted(case_id=env.event_id, shipment=event, decision=decision)
        topic = Topic.PLAYBOOK_QUEUE if decision.route is Route.PLAYBOOK else Topic.AGENT_QUEUE
        return [Outgoing(topic, env.derive(routed, salt="route"))]

    return handle


def playbook_handler(carrier: CarrierClient) -> Handler:
    """Runs the (pure) playbook, then files any claim it decided on with the carrier.

    Transient carrier failures are retried by the runner; a permanent one escalates the
    case rather than telling the customer a claim exists when it does not.
    """

    def handle(env: Envelope) -> Sequence[Outgoing]:
        case = env.parse(ExceptionRouted)
        playbook = PLAYBOOKS.get(case.decision.playbook or "")
        if playbook is None:
            raise PermanentError(f"no playbook named {case.decision.playbook!r}")
        with telemetry.tracer.start_as_current_span(f"playbook {case.decision.playbook}"):
            resolution = playbook(case)
            try:
                resolution = _file_claims(resolution, carrier)
            except CarrierError as exc:
                if exc.transient:
                    raise TransientError(str(exc)) from exc
                escalation = CaseEscalated(
                    case_id=case.case_id,
                    shipment_id=case.shipment.shipment_id,
                    source="playbook",
                    reason=f"claim could not be filed with the carrier: {exc}",
                )
                return [Outgoing(Topic.ESCALATIONS, env.derive(escalation, salt="resolve"))]
        return [Outgoing(Topic.RESOLUTIONS, env.derive(resolution, salt="resolve"))]

    return handle


def _file_claims(resolution: CaseResolved, carrier: CarrierClient) -> CaseResolved:
    actions = []
    for action in resolution.actions:
        if action.kind == "claim" and action.external_ref is None:
            claim = carrier.open_claim(
                resolution.shipment_id,
                float(action.params["amount_gbp"]),
                f"{resolution.case_id}:claim",
            )
            action = action.model_copy(update={"external_ref": claim.claim_id})
        actions.append(action)
    return resolution.model_copy(update={"actions": tuple(actions)})


def agent_handler(agent: ShipmentExceptionAgent, outcomes: OutcomeStore) -> Handler:
    """Runs the agent at most once per case.

    - Outcome stored: a redelivery after the run finished; republish it.
    - Journal entries but no outcome: the last run was interrupted after it started acting;
      escalate with what it did instead of letting a second run choose differently.
    - Neither: first delivery (or a crash before any side effect); run the agent.
    """

    def handle(env: Envelope) -> Sequence[Outgoing]:
        case = env.parse(ExceptionRouted)
        stored = outcomes.get(case.case_id)
        if stored is None:
            started = agent.journal.entries(case.case_id)
            if started:
                telemetry.AGENT_OUTCOMES.labels("interrupted").inc()
                event: CaseResolved | CaseEscalated = interrupted_run(case, started)
            else:
                event = agent.run(case).to_event(case)
            stored = outcomes.put_if_absent(
                case.case_id, StoredOutcome(type(event).__name__, event.model_dump_json())
            )
        else:
            telemetry.AGENT_OUTCOMES.labels("reused").inc()
        final: CaseResolved | CaseEscalated = (
            CaseResolved.model_validate_json(stored.payload_json)
            if stored.event_type == "CaseResolved"
            else CaseEscalated.model_validate_json(stored.payload_json)
        )
        topic = Topic.RESOLUTIONS if isinstance(final, CaseResolved) else Topic.ESCALATIONS
        return [Outgoing(topic, env.derive(final, salt="resolve"))]

    return handle


def interrupted_run(case: ExceptionRouted, started: Sequence[JournalEntry]) -> CaseEscalated:
    confirmed = [e for e in started if e.external_ref is not None]
    unconfirmed = [e for e in started if e.external_ref is None]
    parts = [f"{e.kind} ({e.external_ref})" for e in confirmed] + [
        f"{e.kind} (outcome unknown)" for e in unconfirmed
    ]
    return CaseEscalated(
        case_id=case.case_id,
        shipment_id=case.shipment.shipment_id,
        source="agent",
        reason=(
            "agent run was interrupted after it started acting: "
            + ", ".join(parts)
            + "; check these with the carrier before doing anything else"
        ),
        actions=tuple(
            Action.model_validate(
                {"kind": e.kind, "params": e.params, "external_ref": e.external_ref}
            )
            for e in confirmed
        ),
    )


def notifier_handler(outbox: Outbox, cases: CaseRegistry) -> Handler:
    """Writes the final message for a case, then settles the case for its shipment.

    Resolved: the case is closed. Escalated: the case is held for ops; it stays open, and
    keeps further exceptions for that shipment with humans, until released via ops-api.
    """

    def handle(env: Envelope) -> Sequence[Outgoing]:
        if env.event_type == "CaseResolved":
            r = env.parse(CaseResolved)
            outbox.add(
                Notification(
                    f"{r.case_id}:customer",
                    r.case_id,
                    r.shipment_id,
                    "customer",
                    r.customer_message,
                )
            )
            cases.close(r.case_id)
        elif env.event_type == "CaseEscalated":
            e = env.parse(CaseEscalated)
            outbox.add(
                Notification(
                    f"{e.case_id}:ops",
                    e.case_id,
                    e.shipment_id,
                    "ops",
                    f"Escalated by {e.source}: {e.reason}",
                )
            )
            cases.hold(e.case_id, e.reason)
        else:
            raise PermanentError(f"notifier cannot handle {env.event_type}")
        return []

    return handle
