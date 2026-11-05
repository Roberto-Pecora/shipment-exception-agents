from __future__ import annotations

import pytest

from shipex.agent.guardrails import CaseState, GuardrailPolicy, check
from shipex.agent.tools import (
    ClaimArgs,
    CustomsDocsArgs,
    EscalateArgs,
    RebookArgs,
    ResolveArgs,
    ShipmentArgs,
)
from shipex.contracts.events import ServiceLevel

POLICY = GuardrailPolicy()
MESSAGE = "Your shipment was delayed and we are handling it."


def reefer() -> CaseState:
    return CaseState("c", "SHP-1", 500.0, service_level=ServiceLevel.TEMPERATURE_CONTROLLED)


@pytest.mark.parametrize(
    "args",
    [
        RebookArgs(shipment_id="SHP-1", option_id="opt-air"),
        ClaimArgs(shipment_id="SHP-1", amount_gbp=100),
        CustomsDocsArgs(shipment_id="SHP-1", documents=["invoice"]),
        ResolveArgs(resolution_code="CUSTOMER_NOTIFIED", customer_message=MESSAGE),
    ],
)
def test_temperature_controlled_goods_cannot_be_acted_on(args: object) -> None:
    reason = check(args, reefer(), POLICY)  # type: ignore[arg-type]
    assert reason is not None and "escalate_to_human" in reason


def test_temperature_controlled_goods_can_still_be_read_and_escalated() -> None:
    assert check(ShipmentArgs(shipment_id="SHP-1"), reefer(), POLICY) is None
    assert check(EscalateArgs(reason="spoilage risk"), reefer(), POLICY) is None


def test_standard_goods_are_unaffected() -> None:
    state = CaseState("c", "SHP-1", 500.0)
    assert check(ClaimArgs(shipment_id="SHP-1", amount_gbp=100), state, POLICY) is None
