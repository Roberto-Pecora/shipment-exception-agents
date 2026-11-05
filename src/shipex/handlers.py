"""Per-service business logic as pure `Envelope -> [Outgoing]` handlers.

Handlers know nothing about Kafka, retries or idempotency — `ConsumerRunner` owns
delivery. That split is what lets the same handlers run in the deployed services and
in the in-process pipeline used by tests and evals.
"""

from __future__ import annotations

from collections.abc import Sequence

from shipex import telemetry
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.bus.consumer import Handler, Outgoing, PermanentError
from shipex.contracts.events import (
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
from shipex.store.outbox import Notification, Outbox

SUBSCRIPTIONS: dict[str, tuple[Topic, ...]] = {
    "router": (Topic.SHIPMENT_EVENTS,),
    "playbook": (Topic.PLAYBOOK_QUEUE,),
    "agent": (Topic.AGENT_QUEUE,),
    "notifier": (Topic.RESOLUTIONS, Topic.ESCALATIONS),
}


def router_handler(policy: RoutingPolicy | None = None, agent_only: bool = False) -> Handler:
    """`agent_only=True` is the eval baseline: every exception goes to the LLM."""

    def handle(env: Envelope) -> Sequence[Outgoing]:
        event = env.parse(ShipmentEvent)
        with telemetry.tracer.start_as_current_span("route") as span:
            decision = route(event, policy)
            if agent_only and decision.route is not Route.IGNORE:
                decision = RoutingDecision(
                    route=Route.AGENT, rule_id="agent_only", reason="baseline: agent handles all"
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


def playbook_handler() -> Handler:
    def handle(env: Envelope) -> Sequence[Outgoing]:
        case = env.parse(ExceptionRouted)
        playbook = PLAYBOOKS.get(case.decision.playbook or "")
        if playbook is None:
            raise PermanentError(f"no playbook named {case.decision.playbook!r}")
        with telemetry.tracer.start_as_current_span(f"playbook {case.decision.playbook}"):
            resolution = playbook(case)
        return [Outgoing(Topic.RESOLUTIONS, env.derive(resolution, salt="resolve"))]

    return handle


def agent_handler(agent: ShipmentExceptionAgent) -> Handler:
    def handle(env: Envelope) -> Sequence[Outgoing]:
        case = env.parse(ExceptionRouted)
        event = agent.run(case).to_event(case)
        topic = Topic.RESOLUTIONS if isinstance(event, CaseResolved) else Topic.ESCALATIONS
        return [Outgoing(topic, env.derive(event, salt="resolve"))]

    return handle


def notifier_handler(outbox: Outbox) -> Handler:
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
        else:
            raise PermanentError(f"notifier cannot handle {env.event_type}")
        return []

    return handle
