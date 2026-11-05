"""Deterministic action policy, enforced in code before any tool executes (docs/adr/0003).

The prompt *describes* these rules; this module *enforces* them. A blocked call is
returned to the model as a tool result explaining why, so it can choose another path
(usually escalation) — and every block is counted in metrics and eval reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shipex.agent.carrier import RebookingOption
from shipex.agent.tools import ClaimArgs, CustomsDocsArgs, RebookArgs, ResolveArgs, ToolArgs
from shipex.contracts.events import Action, ServiceLevel


@dataclass(frozen=True)
class GuardrailPolicy:
    max_rebook_cost_delta_gbp: float = 150.0
    max_agent_claim_gbp: float = 2_000.0
    forbidden_phrases: tuple[str, ...] = ("refund", "compensation", "guarantee")


@dataclass
class CaseState:
    case_id: str
    shipment_id: str
    declared_value_gbp: float
    service_level: ServiceLevel = ServiceLevel.STANDARD
    offered_options: dict[str, RebookingOption] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)

    def has(self, kind: str) -> bool:
        return any(a.kind == kind for a in self.actions)


_RESOLUTION_REQUIRES = {
    "REBOOKED": "rebook",
    "CUSTOMS_DOCS_REQUESTED": "customs_docs_request",
    "CLAIM_OPENED": "claim",
}


def check(args: ToolArgs, state: CaseState, policy: GuardrailPolicy) -> str | None:
    """Return a block reason, or None if the call may proceed."""
    target = getattr(args, "shipment_id", state.shipment_id)
    if target != state.shipment_id:
        return f"tools may only act on shipment {state.shipment_id}, not {target}"

    if state.service_level is ServiceLevel.TEMPERATURE_CONTROLLED and isinstance(
        args, RebookArgs | ClaimArgs | CustomsDocsArgs | ResolveArgs
    ):
        return (
            "temperature-controlled goods may not be rebooked, claimed or resolved by the "
            "agent: call escalate_to_human"
        )

    if isinstance(args, RebookArgs):
        if state.has("rebook"):
            return "this shipment was already rebooked in this case"
        option = state.offered_options.get(args.option_id)
        if option is None:
            return (
                f"option_id {args.option_id!r} was not offered; call get_rebooking_options "
                "and pick one of the returned option_ids"
            )
        if option.cost_delta_gbp > policy.max_rebook_cost_delta_gbp:
            return (
                f"rebooking cost delta £{option.cost_delta_gbp:.2f} exceeds the "
                f"£{policy.max_rebook_cost_delta_gbp:.2f} cap; escalate_to_human instead"
            )

    elif isinstance(args, ClaimArgs):
        if state.has("claim"):
            return "a claim was already opened in this case"
        if args.amount_gbp > state.declared_value_gbp:
            return (
                f"claim £{args.amount_gbp:.2f} exceeds the declared value "
                f"£{state.declared_value_gbp:.2f}"
            )
        if args.amount_gbp > policy.max_agent_claim_gbp:
            return (
                f"claims above £{policy.max_agent_claim_gbp:.2f} need human approval; "
                "escalate_to_human instead"
            )

    elif isinstance(args, CustomsDocsArgs):
        if state.has("customs_docs_request"):
            return "customs documents were already requested in this case"

    elif isinstance(args, ResolveArgs):
        lowered = args.customer_message.lower()
        for phrase in policy.forbidden_phrases:
            if phrase in lowered:
                return f"customer message must not mention {phrase!r}; remove the promise"
        required = _RESOLUTION_REQUIRES.get(args.resolution_code)
        if required and not state.has(required):
            return (
                f"resolution {args.resolution_code} requires a successful {required} action "
                "in this case, and none was taken"
            )

    return None
