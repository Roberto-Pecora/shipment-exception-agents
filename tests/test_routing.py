from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from shipex.contracts.events import Route, ServiceLevel, StatusCode
from shipex.playbooks import auto_claim, re_eta_notify
from shipex.routing.rules import RoutingPolicy, route
from tests.conftest import NOW, make_case, make_event


@pytest.mark.parametrize(
    ("overrides", "expected_route", "expected_rule"),
    [
        ({"status_code": StatusCode.IN_TRANSIT}, Route.IGNORE, "routine_scan"),
        ({"status_code": StatusCode.DELIVERED}, Route.IGNORE, "routine_scan"),
        ({"declared_value_gbp": 25_000.0}, Route.HUMAN, "high_value"),
        ({"status_code": StatusCode.LOST}, Route.HUMAN, "lost_shipment"),
        ({}, Route.PLAYBOOK, "delay_within_sla"),
        ({"status_code": StatusCode.ADDRESS_ISSUE}, Route.PLAYBOOK, "address_issue"),
        (
            {"status_code": StatusCode.DAMAGED, "declared_value_gbp": 120.0},
            Route.PLAYBOOK,
            "low_value_damage",
        ),
        # Everything below needs judgement, so it reaches the agent.
        ({"delay_hours": 30.0}, Route.AGENT, "needs_judgement"),
        ({"eta": None}, Route.AGENT, "needs_judgement"),
        ({"sla_due_at": NOW + timedelta(hours=20)}, Route.AGENT, "needs_judgement"),
        (
            {"service_level": ServiceLevel.TEMPERATURE_CONTROLLED},
            Route.AGENT,
            "needs_judgement",
        ),
        ({"status_code": StatusCode.CUSTOMS_HOLD}, Route.AGENT, "needs_judgement"),
        ({"status_code": StatusCode.DAMAGED}, Route.AGENT, "needs_judgement"),
        ({"status_code": StatusCode.EXCEPTION_OTHER}, Route.AGENT, "needs_judgement"),
    ],
)
def test_policy_table(overrides: dict[str, Any], expected_route: Route, expected_rule: str) -> None:
    decision = route(make_event(**overrides))
    assert (decision.route, decision.rule_id) == (expected_route, expected_rule)


def test_first_match_wins_high_value_beats_auto_claim() -> None:
    event = make_event(status_code=StatusCode.DAMAGED, declared_value_gbp=50_000.0)
    assert route(event).rule_id == "high_value"


def test_policy_thresholds_are_configurable() -> None:
    event = make_event(status_code=StatusCode.DAMAGED, declared_value_gbp=800.0)
    assert route(event).route is Route.AGENT
    assert route(event, RoutingPolicy(auto_claim_limit_gbp=1_000.0)).rule_id == "low_value_damage"


def test_re_eta_playbook_computes_new_eta() -> None:
    event = make_event()
    resolution = re_eta_notify(make_case(event))
    assert resolution.resolved_by == "playbook"
    assert resolution.actions[0].params["new_eta"] == (NOW + timedelta(days=1, hours=6)).isoformat()
    assert "6 hours late (weather)" in resolution.customer_message


def test_auto_claim_uses_declared_value() -> None:
    event = make_event(status_code=StatusCode.DAMAGED, declared_value_gbp=120.0)
    resolution = auto_claim(make_case(event))
    assert resolution.actions[0].params == {"amount_gbp": 120.0}
