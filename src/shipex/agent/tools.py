"""Tool surface exposed to the model.

Arguments are pydantic models: the same class produces the JSON schema sent to the
model and validates what comes back, so a schema drift is impossible by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolKind(StrEnum):
    READ = "read"
    SIDE_EFFECT = "side_effect"
    TERMINAL = "terminal"


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ShipmentArgs(ToolArgs):
    shipment_id: str


class RebookArgs(ToolArgs):
    shipment_id: str
    option_id: str


class CustomsDocsArgs(ToolArgs):
    shipment_id: str
    documents: list[str] = Field(min_length=1, max_length=5)


class ClaimArgs(ToolArgs):
    shipment_id: str
    amount_gbp: float = Field(gt=0)


AgentResolution = Literal["CUSTOMER_NOTIFIED", "REBOOKED", "CUSTOMS_DOCS_REQUESTED", "CLAIM_OPENED"]


class ResolveArgs(ToolArgs):
    resolution_code: AgentResolution
    customer_message: str = Field(min_length=20, max_length=600)


class EscalateArgs(ToolArgs):
    reason: str = Field(min_length=5, max_length=300)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args: type[ToolArgs]
    kind: ToolKind


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "get_tracking",
            "Current carrier tracking for the shipment: status, location, ETA, scan history "
            "and any customs documents the broker reports as missing.",
            ShipmentArgs,
            ToolKind.READ,
        ),
        ToolSpec(
            "get_rebooking_options",
            "Faster services the carrier can move the shipment onto, with new ETA and the "
            "cost difference in GBP. Call before rebook_shipment.",
            ShipmentArgs,
            ToolKind.READ,
        ),
        ToolSpec(
            "rebook_shipment",
            "Move the shipment onto one of the offered rebooking options. Irreversible.",
            RebookArgs,
            ToolKind.SIDE_EFFECT,
        ),
        ToolSpec(
            "request_customs_documents",
            "Ask the shipper for the customs documents needed to release a held shipment.",
            CustomsDocsArgs,
            ToolKind.SIDE_EFFECT,
        ),
        ToolSpec(
            "open_damage_claim",
            "Open a damage claim with the carrier for the given amount in GBP.",
            ClaimArgs,
            ToolKind.SIDE_EFFECT,
        ),
        ToolSpec(
            "resolve_case",
            "Finish the case: give the resolution code and the message the customer will "
            "receive. Only codes backed by an action you actually took are accepted.",
            ResolveArgs,
            ToolKind.TERMINAL,
        ),
        ToolSpec(
            "escalate_to_human",
            "Finish the case by handing it to an operations specialist, with the reason.",
            EscalateArgs,
            ToolKind.TERMINAL,
        ),
    )
}


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI-style function definitions, in a stable order (stable prompt prefix)."""
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.args.model_json_schema(),
            },
        }
        for spec in TOOLS.values()
    ]
