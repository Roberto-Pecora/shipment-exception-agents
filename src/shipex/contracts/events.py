"""Versioned event contracts shared by every service.

Every message on the bus is an `Envelope`: transport metadata (ids, causation chain,
schema version) wrapping one typed payload. Business logic only sees payloads; the
envelope is what makes delivery idempotent and traceable end to end.

Event ids are deterministic: the ingest event id is derived from the carrier's own
event id, and every downstream id is derived from its cause. A handler that is re-run
after a crash therefore re-emits the *same* ids, and consumers dedupe them.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

NAMESPACE = uuid.UUID("6f1c2a1e-6f0b-4f43-9a51-2d7d4b2b8e10")

P = TypeVar("P", bound=BaseModel)


class Topic(StrEnum):
    SHIPMENT_EVENTS = "shipment.events.v1"
    PLAYBOOK_QUEUE = "exceptions.playbook.v1"
    AGENT_QUEUE = "exceptions.agent.v1"
    RESOLUTIONS = "resolutions.v1"
    ESCALATIONS = "escalations.v1"


def dlq_topic(topic: str) -> str:
    return f"{topic}.dlq"


class StatusCode(StrEnum):
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    DELAYED = "DELAYED"
    CUSTOMS_HOLD = "CUSTOMS_HOLD"
    DAMAGED = "DAMAGED"
    ADDRESS_ISSUE = "ADDRESS_ISSUE"
    LOST = "LOST"
    EXCEPTION_OTHER = "EXCEPTION_OTHER"


ROUTINE_STATUSES = frozenset(
    {
        StatusCode.PICKED_UP,
        StatusCode.IN_TRANSIT,
        StatusCode.OUT_FOR_DELIVERY,
        StatusCode.DELIVERED,
    }
)


class ServiceLevel(StrEnum):
    STANDARD = "standard"
    EXPRESS = "express"
    TEMPERATURE_CONTROLLED = "temperature_controlled"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ShipmentEvent(_Frozen):
    """A carrier status update, as received on the webhook."""

    carrier_event_id: str = Field(min_length=1, max_length=128)
    shipment_id: str = Field(min_length=1, max_length=64)
    carrier: str = Field(min_length=1, max_length=64)
    status_code: StatusCode
    occurred_at: datetime
    reason: str | None = Field(default=None, max_length=500)
    delay_hours: float | None = Field(default=None, ge=0)
    declared_value_gbp: float = Field(ge=0)
    service_level: ServiceLevel
    sla_due_at: datetime
    eta: datetime | None = None
    location: str | None = Field(default=None, max_length=128)


class Route(StrEnum):
    IGNORE = "ignore"
    PLAYBOOK = "playbook"
    AGENT = "agent"
    HUMAN = "human"


class RoutingDecision(_Frozen):
    route: Route
    rule_id: str
    reason: str
    playbook: str | None = None


class ExceptionRouted(_Frozen):
    case_id: str
    shipment: ShipmentEvent
    decision: RoutingDecision


class ResolutionCode(StrEnum):
    CUSTOMER_NOTIFIED = "CUSTOMER_NOTIFIED"
    REBOOKED = "REBOOKED"
    CUSTOMS_DOCS_REQUESTED = "CUSTOMS_DOCS_REQUESTED"
    CLAIM_OPENED = "CLAIM_OPENED"
    ADDRESS_CONFIRMATION_REQUESTED = "ADDRESS_CONFIRMATION_REQUESTED"


class Action(_Frozen):
    """A side effect taken (or requested) while handling a case."""

    kind: Literal["notify", "rebook", "customs_docs_request", "claim", "address_confirmation"]
    params: dict[str, Any] = Field(default_factory=dict)
    external_ref: str | None = None


class RunStats(_Frozen):
    """Cost/latency/reliability counters for one agent run."""

    model: str
    steps: int
    tool_calls: int
    invalid_tool_calls: int
    guardrail_blocks: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: float


class CaseResolved(_Frozen):
    case_id: str
    shipment_id: str
    resolved_by: Literal["playbook", "agent"]
    resolution_code: ResolutionCode
    actions: tuple[Action, ...]
    customer_message: str
    run: RunStats | None = None


class CaseEscalated(_Frozen):
    case_id: str
    shipment_id: str
    source: Literal["router", "agent"]
    reason: str
    actions: tuple[Action, ...] = ()
    run: RunStats | None = None


EVENT_TYPES: dict[str, type[BaseModel]] = {
    "ShipmentEventReceived": ShipmentEvent,
    "ExceptionRouted": ExceptionRouted,
    "CaseResolved": CaseResolved,
    "CaseEscalated": CaseEscalated,
}
_TYPE_NAMES = {model: name for name, model in EVENT_TYPES.items()}


class Envelope(_Frozen):
    event_id: str
    event_type: str
    schema_version: int = 1
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None = None
    key: str
    payload: dict[str, Any]

    @classmethod
    def for_shipment_event(cls, event: ShipmentEvent) -> Envelope:
        """Ingest envelope; the id is a pure function of the carrier's event id."""
        event_id = str(uuid.uuid5(NAMESPACE, f"{event.carrier}:{event.carrier_event_id}"))
        return cls(
            event_id=event_id,
            event_type="ShipmentEventReceived",
            occurred_at=datetime.now(UTC),
            correlation_id=event_id,
            key=event.shipment_id,
            payload=event.model_dump(mode="json"),
        )

    def derive(self, payload: BaseModel, *, salt: str) -> Envelope:
        """Child event caused by this one, with a deterministic id."""
        return Envelope(
            event_id=str(uuid.uuid5(NAMESPACE, f"{self.event_id}:{salt}")),
            event_type=_TYPE_NAMES[type(payload)],
            occurred_at=datetime.now(UTC),
            correlation_id=self.correlation_id,
            causation_id=self.event_id,
            key=self.key,
            payload=payload.model_dump(mode="json"),
        )

    def parse(self, model: type[P]) -> P:
        expected = EVENT_TYPES.get(self.event_type)
        if expected is not model:
            raise ValueError(f"event_type {self.event_type!r} is not a {model.__name__}")
        return model.model_validate(self.payload)

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> Envelope:
        return cls.model_validate(json.loads(raw))
