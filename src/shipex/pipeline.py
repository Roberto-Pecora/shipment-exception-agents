"""The whole service graph in one process, on the in-memory bus.

Same handlers and `ConsumerRunner` as the deployed workers — only the transport and
stores are swapped — so tests and offline evals exercise the real delivery semantics.
"""

from __future__ import annotations

from shipex.agent.loop import ShipmentExceptionAgent
from shipex.bus.consumer import ConsumerRunner, Handler, RetryPolicy
from shipex.bus.memory import InMemoryBus
from shipex.contracts.events import Envelope, ShipmentEvent, Topic
from shipex.handlers import (
    SUBSCRIPTIONS,
    agent_handler,
    notifier_handler,
    playbook_handler,
    router_handler,
)
from shipex.routing.rules import RoutingPolicy
from shipex.store.idempotency import InMemoryIdempotencyStore
from shipex.store.outbox import InMemoryOutbox


class InProcessPipeline:
    def __init__(
        self,
        agent: ShipmentExceptionAgent,
        *,
        policy: RoutingPolicy | None = None,
        agent_only: bool = False,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.bus = InMemoryBus()
        self.outbox = InMemoryOutbox()
        self.idempotency = InMemoryIdempotencyStore()
        handlers: dict[str, Handler] = {
            "router": router_handler(policy, agent_only=agent_only),
            "playbook": playbook_handler(),
            "agent": agent_handler(agent),
            "notifier": notifier_handler(self.outbox),
        }
        self.runners = [
            ConsumerRunner(
                name,
                self.bus.subscriber(name, SUBSCRIPTIONS[name]),
                self.bus,
                handler,
                self.idempotency,
                retry or RetryPolicy(max_attempts=2),
                sleep=lambda _s: None,
            )
            for name, handler in handlers.items()
        ]

    def submit(self, event: ShipmentEvent) -> Envelope:
        env = Envelope.for_shipment_event(event)
        self.bus.publish(Topic.SHIPMENT_EVENTS, env.key, env.to_bytes())
        return env

    def drain(self, max_records: int = 10_000) -> int:
        """Run every consumer until no records are left; return how many were handled."""
        handled = 0
        progressed = True
        while progressed and handled < max_records:
            progressed = False
            for runner in self.runners:
                while runner.run_once(0) is not None:
                    handled += 1
                    progressed = True
        return handled
