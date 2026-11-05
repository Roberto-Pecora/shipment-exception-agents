from __future__ import annotations

import pytest
from pydantic import ValidationError

from shipex.contracts.events import CaseEscalated, Envelope, ExceptionRouted, ShipmentEvent
from tests.conftest import make_event


def test_ingest_id_is_a_function_of_the_carrier_event_id() -> None:
    a = Envelope.for_shipment_event(make_event())
    b = Envelope.for_shipment_event(make_event())
    c = Envelope.for_shipment_event(make_event(carrier_event_id="evt-2"))
    assert a.event_id == b.event_id
    assert a.event_id != c.event_id
    assert a.correlation_id == a.event_id
    assert a.key == "SHP-1"


def test_derived_events_have_deterministic_ids_and_causation() -> None:
    parent = Envelope.for_shipment_event(make_event())
    esc = CaseEscalated(case_id="c", shipment_id="SHP-1", source="router", reason="r")
    child1 = parent.derive(esc, salt="route")
    child2 = parent.derive(esc, salt="route")
    assert child1.event_id == child2.event_id
    assert child1.causation_id == parent.event_id
    assert child1.correlation_id == parent.correlation_id
    assert child1.event_type == "CaseEscalated"


def test_round_trip_and_typed_parse() -> None:
    env = Envelope.for_shipment_event(make_event())
    again = Envelope.from_bytes(env.to_bytes())
    assert again.parse(ShipmentEvent) == make_event()
    with pytest.raises(ValueError, match="not a ExceptionRouted"):
        again.parse(ExceptionRouted)


def test_contract_rejects_unknown_fields() -> None:
    data = make_event().model_dump()
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        ShipmentEvent.model_validate(data)
