"""Shared factories. Tests are hermetic: no broker, no database, no API key, no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shipex.agent.carrier import CarrierFixture, InMemoryCarrier, RebookingOption, TrackingInfo
from shipex.contracts.events import (
    ExceptionRouted,
    Route,
    RoutingDecision,
    ServiceLevel,
    ShipmentEvent,
    StatusCode,
)

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


def make_event(**overrides: Any) -> ShipmentEvent:
    fields: dict[str, Any] = {
        "carrier_event_id": "evt-1",
        "shipment_id": "SHP-1",
        "carrier": "acme-freight",
        "status_code": StatusCode.DELAYED,
        "occurred_at": NOW,
        "reason": "weather",
        "delay_hours": 6.0,
        "declared_value_gbp": 800.0,
        "service_level": ServiceLevel.STANDARD,
        "sla_due_at": NOW + timedelta(days=3),
        "eta": NOW + timedelta(days=1),
        "location": "Leipzig hub",
    }
    fields.update(overrides)
    return ShipmentEvent(**fields)


def make_case(event: ShipmentEvent | None = None, case_id: str = "case-1") -> ExceptionRouted:
    return ExceptionRouted(
        case_id=case_id,
        shipment=event or make_event(status_code=StatusCode.CUSTOMS_HOLD, delay_hours=None),
        decision=RoutingDecision(route=Route.AGENT, rule_id="needs_judgement", reason="test"),
    )


def make_carrier(shipment_id: str = "SHP-1", **tracking: Any) -> InMemoryCarrier:
    info: dict[str, Any] = {
        "shipment_id": shipment_id,
        "status": "DELAYED",
        "location": "Leipzig hub",
        "eta": NOW + timedelta(days=4),
        "history": ["picked up", "arrived Leipzig hub", "missed linehaul"],
    }
    info.update(tracking)
    options = [
        RebookingOption(
            option_id="opt-air",
            service="air express",
            new_eta=NOW + timedelta(days=1),
            cost_delta_gbp=95.0,
        ),
        RebookingOption(
            option_id="opt-charter",
            service="dedicated charter",
            new_eta=NOW + timedelta(hours=10),
            cost_delta_gbp=640.0,
        ),
    ]
    return InMemoryCarrier(
        {shipment_id: CarrierFixture(tracking=TrackingInfo(**info), rebooking_options=options)}
    )


@pytest.fixture
def carrier() -> InMemoryCarrier:
    return make_carrier()
