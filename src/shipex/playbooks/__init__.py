"""Deterministic playbooks: fixed pipelines for exceptions a rule can fully specify.

Each playbook is a pure function of the routed case, so it is trivially testable, costs
nothing per run, and produces the same output on redelivery.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from shipex.contracts.events import Action, CaseResolved, ExceptionRouted, ResolutionCode

Playbook = Callable[[ExceptionRouted], CaseResolved]


def re_eta_notify(case: ExceptionRouted) -> CaseResolved:
    s = case.shipment
    assert s.eta is not None and s.delay_hours is not None  # guaranteed by delay_within_sla
    new_eta = s.eta + timedelta(hours=s.delay_hours)
    cause = f" ({s.reason})" if s.reason else ""
    message = (
        f"Your shipment {s.shipment_id} is running about {s.delay_hours:g} hours late{cause}. "
        f"New estimated delivery: {new_eta:%a %d %b, %H:%M} UTC. No action is needed from you."
    )
    return CaseResolved(
        case_id=case.case_id,
        shipment_id=s.shipment_id,
        resolved_by="playbook",
        resolution_code=ResolutionCode.CUSTOMER_NOTIFIED,
        actions=(Action(kind="notify", params={"new_eta": new_eta.isoformat()}),),
        customer_message=message,
    )


def address_confirmation(case: ExceptionRouted) -> CaseResolved:
    s = case.shipment
    message = (
        f"We couldn't deliver shipment {s.shipment_id} because of an address problem. "
        "Please confirm or correct the delivery address so we can rebook delivery."
    )
    return CaseResolved(
        case_id=case.case_id,
        shipment_id=s.shipment_id,
        resolved_by="playbook",
        resolution_code=ResolutionCode.ADDRESS_CONFIRMATION_REQUESTED,
        actions=(Action(kind="address_confirmation"),),
        customer_message=message,
    )


def auto_claim(case: ExceptionRouted) -> CaseResolved:
    s = case.shipment
    message = (
        f"Shipment {s.shipment_id} was reported damaged in transit. We have opened a damage "
        "claim on your behalf and will contact you with the outcome."
    )
    return CaseResolved(
        case_id=case.case_id,
        shipment_id=s.shipment_id,
        resolved_by="playbook",
        resolution_code=ResolutionCode.CLAIM_OPENED,
        actions=(Action(kind="claim", params={"amount_gbp": s.declared_value_gbp}),),
        customer_message=message,
    )


PLAYBOOKS: dict[str, Playbook] = {
    "re_eta_notify": re_eta_notify,
    "address_confirmation": address_confirmation,
    "auto_claim": auto_claim,
}
