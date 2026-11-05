"""Deterministic-first routing (docs/adr/0002).

An ordered policy table, first match wins. Anything a rule can decide with certainty
goes to a deterministic playbook (cheap, fast, auditable) or straight to a human
(high value / high risk). Only what no rule can settle reaches the LLM agent, and the
decision carries a machine-readable `rule_id` so routing is measurable and reviewable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from shipex.contracts.events import (
    ROUTINE_STATUSES,
    Route,
    RoutingDecision,
    ServiceLevel,
    ShipmentEvent,
    StatusCode,
)


@dataclass(frozen=True)
class RoutingPolicy:
    high_value_gbp: float = 10_000.0
    auto_claim_limit_gbp: float = 500.0
    max_playbook_delay_hours: float = 24.0


def breaches_sla(event: ShipmentEvent) -> bool | None:
    """True/False if the delayed ETA can be computed against the SLA, else None."""
    if event.eta is None or event.delay_hours is None:
        return None
    return event.eta + timedelta(hours=event.delay_hours) > event.sla_due_at


def _simple_delay(e: ShipmentEvent, p: RoutingPolicy) -> bool:
    return (
        e.status_code is StatusCode.DELAYED
        and e.delay_hours is not None
        and e.delay_hours <= p.max_playbook_delay_hours
        and e.service_level is not ServiceLevel.TEMPERATURE_CONTROLLED
        and breaches_sla(e) is False
    )


@dataclass(frozen=True)
class Rule:
    rule_id: str
    route: Route
    reason: str
    when: Callable[[ShipmentEvent, RoutingPolicy], bool]
    playbook: str | None = None


RULES: tuple[Rule, ...] = (
    Rule(
        "routine_scan",
        Route.IGNORE,
        "routine status update, no exception",
        lambda e, p: e.status_code in ROUTINE_STATUSES,
    ),
    Rule(
        "high_value",
        Route.HUMAN,
        "declared value at or above the high-value threshold",
        lambda e, p: e.declared_value_gbp >= p.high_value_gbp,
    ),
    Rule(
        "lost_shipment",
        Route.HUMAN,
        "lost shipments need an investigation, not automation",
        lambda e, p: e.status_code is StatusCode.LOST,
    ),
    Rule(
        "delay_within_sla",
        Route.PLAYBOOK,
        "short delay that still meets the SLA: re-ETA and notify",
        _simple_delay,
        playbook="re_eta_notify",
    ),
    Rule(
        "address_issue",
        Route.PLAYBOOK,
        "address problems are resolved by asking the consignee",
        lambda e, p: e.status_code is StatusCode.ADDRESS_ISSUE,
        playbook="address_confirmation",
    ),
    Rule(
        "low_value_damage",
        Route.PLAYBOOK,
        "damage below the auto-claim limit: open a claim for declared value",
        lambda e, p: (
            e.status_code is StatusCode.DAMAGED and e.declared_value_gbp <= p.auto_claim_limit_gbp
        ),
        playbook="auto_claim",
    ),
)

FALLBACK = Rule(
    "needs_judgement",
    Route.AGENT,
    "no deterministic rule applies; needs multi-step investigation",
    lambda e, p: True,
)


def route(event: ShipmentEvent, policy: RoutingPolicy | None = None) -> RoutingDecision:
    policy = policy or RoutingPolicy()
    rule = next((r for r in RULES if r.when(event, policy)), FALLBACK)
    return RoutingDecision(
        route=rule.route, rule_id=rule.rule_id, reason=rule.reason, playbook=rule.playbook
    )
