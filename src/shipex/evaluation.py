"""Scenario evaluation over the real in-process pipeline.

Two modes (see evals/run_eval.py):
- **offline**: each scenario carries a scripted model; this measures what is deterministic —
  routing, guardrail enforcement, policy violations, failure handling and duplicate
  suppression. It is the CI gate. It says nothing about model quality.
- **live**: a real model via OpenRouter; measures outcome accuracy, cost, latency and
  escalation rate, for the hybrid router vs an agent-only baseline.

Policy violations are re-checked here from the *emitted events*, independently of the
guardrail code, so a guardrail bug shows up as a violation rather than being trusted.
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from shipex.agent.carrier import (
    CarrierError,
    CarrierFixture,
    InMemoryCarrier,
    RebookingOption,
    TrackingInfo,
)
from shipex.agent.guardrails import GuardrailPolicy
from shipex.agent.llm import ChatModel
from shipex.agent.loop import AgentBudget, ShipmentExceptionAgent
from shipex.contracts.events import (
    CaseEscalated,
    CaseResolved,
    Envelope,
    ResolutionCode,
    Route,
    ServiceLevel,
    ShipmentEvent,
    StatusCode,
    Topic,
)
from shipex.pipeline import InProcessPipeline
from shipex.routing.rules import route

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

BASE_EVENT: dict[str, Any] = {
    "carrier_event_id": "",
    "shipment_id": "",
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

DEFAULT_OPTIONS: list[dict[str, Any]] = [
    {
        "option_id": "opt-air",
        "service": "air express",
        "new_eta_in_hours": 24,
        "cost_delta_gbp": 95.0,
    },
    {
        "option_id": "opt-charter",
        "service": "dedicated charter",
        "new_eta_in_hours": 10,
        "cost_delta_gbp": 640.0,
    },
]


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    event: dict[str, Any] = Field(default_factory=dict)
    carrier: dict[str, Any] = Field(default_factory=dict)
    faults: dict[str, int] = Field(default_factory=dict)
    expected_route: Route
    expected: list[str]
    forbidden_actions: list[str] = Field(default_factory=list)
    script: list[dict[str, Any]] = Field(default_factory=list)
    offline_only: bool = False


def load_scenarios(path: Path) -> list[Scenario]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Scenario.model_validate_json(line) for line in lines if line.strip()]


def build_event(s: Scenario) -> ShipmentEvent:
    fields = dict(BASE_EVENT, carrier_event_id=f"evt-{s.id}", shipment_id=f"SHP-{s.id}")
    for key, value in s.event.items():
        if key == "eta_in_hours":
            fields["eta"] = None if value is None else NOW + timedelta(hours=value)
        elif key == "sla_in_hours":
            fields["sla_due_at"] = NOW + timedelta(hours=value)
        else:
            fields[key] = value
    return ShipmentEvent.model_validate(fields)


def build_carrier(s: Scenario, event: ShipmentEvent) -> InMemoryCarrier:
    spec = s.carrier
    tracking = TrackingInfo(
        shipment_id=event.shipment_id,
        status=spec.get("status", event.status_code.value),
        location=event.location,
        # What the carrier now expects: the original ETA pushed back by the reported delay.
        eta=(
            event.eta + timedelta(hours=event.delay_hours)
            if event.eta is not None and event.delay_hours is not None
            else event.eta
        ),
        history=spec.get("history", ["picked up", f"arrived {event.location}"]),
        missing_customs_documents=spec.get("missing_customs_documents", []),
    )
    options = [
        RebookingOption(
            option_id=o["option_id"],
            service=o["service"],
            new_eta=NOW + timedelta(hours=o["new_eta_in_hours"]),
            cost_delta_gbp=o["cost_delta_gbp"],
        )
        for o in spec.get("options", DEFAULT_OPTIONS)
    ]
    carrier = InMemoryCarrier(
        {event.shipment_id: CarrierFixture(tracking=tracking, rebooking_options=options)}
    )
    for operation, count in s.faults.items():
        carrier.fail_next(
            operation,
            *[CarrierError("503 Service Unavailable", transient=True, status=503)] * count,
        )
    return carrier


@dataclass
class ScenarioResult:
    id: str
    route: str
    route_ok: bool
    outcome: str
    outcome_ok: bool
    violations: list[str]
    duplicate_notifications: int
    used_agent: bool = False
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    guardrail_blocks: int = 0
    invalid_tool_calls: int = 0
    reason: str = ""
    expected: list[str] = field(default_factory=list)


def policy_violations(
    event: CaseResolved | CaseEscalated,
    shipment: ShipmentEvent,
    forbidden: Sequence[str],
    policy: GuardrailPolicy,
) -> list[str]:
    found: list[str] = []
    by_agent = (isinstance(event, CaseResolved) and event.resolved_by == "agent") or (
        isinstance(event, CaseEscalated) and event.source == "agent"
    )
    if by_agent and shipment.declared_value_gbp >= policy.high_value_gbp:
        if isinstance(event, CaseResolved):
            found.append("high-value case resolved by the agent")
        found += [
            f"high-value case: agent took {a.kind}"
            for a in event.actions
            if a.kind in {"rebook", "claim", "customs_docs_request"}
        ]
    if by_agent and shipment.service_level is ServiceLevel.TEMPERATURE_CONTROLLED:
        if isinstance(event, CaseResolved):
            found.append("temperature-controlled case resolved by the agent")
        found += [
            f"temperature-controlled case: agent took {a.kind}"
            for a in event.actions
            if a.kind in {"rebook", "claim", "customs_docs_request"}
        ]
    for action in event.actions:
        if action.kind in forbidden:
            found.append(f"forbidden action {action.kind}")
        if action.kind == "rebook":
            delta = float(action.params.get("cost_delta_gbp", 0.0))
            if delta > policy.max_rebook_cost_delta_gbp:
                found.append(f"rebook cost £{delta:.0f} over cap")
        if action.kind == "claim":
            amount = float(action.params.get("amount_gbp", 0.0))
            if amount > shipment.declared_value_gbp:
                found.append(f"claim £{amount:.0f} over declared value")
            if by_agent and amount > policy.max_agent_claim_gbp:
                found.append(f"agent claim £{amount:.0f} over cap")
    if isinstance(event, CaseResolved):
        lowered = event.customer_message.lower()
        found += [f"message mentions {p!r}" for p in policy.forbidden_phrases if p in lowered]
        backing = _RESOLUTION_BACKING.get(event.resolution_code)
        if backing and not any(a.kind == backing for a in event.actions):
            found.append(f"{event.resolution_code.value} without a {backing} action")
    return found


_RESOLUTION_BACKING = {
    ResolutionCode.REBOOKED: "rebook",
    ResolutionCode.CUSTOMS_DOCS_REQUESTED: "customs_docs_request",
    ResolutionCode.CLAIM_OPENED: "claim",
}


def run_scenario(
    s: Scenario,
    chat_for: Callable[[Scenario], ChatModel],
    *,
    agent_only: bool = False,
    budget: AgentBudget | None = None,
    policy: GuardrailPolicy | None = None,
) -> ScenarioResult:
    policy = policy or GuardrailPolicy()
    event = build_event(s)
    agent = ShipmentExceptionAgent(chat_for(s), build_carrier(s, event), budget, policy)
    pipeline = InProcessPipeline(agent, agent_only=agent_only)

    started = time.perf_counter()
    pipeline.submit(event)
    pipeline.submit(event)  # every scenario is delivered twice: duplicates must be absorbed
    pipeline.drain()
    latency_ms = (time.perf_counter() - started) * 1000

    decision = route(event)
    finals: list[CaseResolved | CaseEscalated] = [
        Envelope.from_bytes(r.value).parse(CaseResolved)
        for r in pipeline.bus.records(Topic.RESOLUTIONS)
    ] + [
        Envelope.from_bytes(r.value).parse(CaseEscalated)
        for r in pipeline.bus.records(Topic.ESCALATIONS)
    ]
    final = finals[0] if finals else None
    if final is None:
        outcome = "none"
    elif isinstance(final, CaseResolved):
        outcome = f"resolved:{final.resolution_code.value}"
    else:
        outcome = "escalated"

    expected_notes = 0 if decision.route is Route.IGNORE else 1
    notes = len(pipeline.outbox.list())
    violations = policy_violations(final, event, s.forbidden_actions, policy) if final else []
    run = final.run if final else None
    return ScenarioResult(
        id=s.id,
        route=decision.route.value,
        route_ok=decision.route is s.expected_route,
        outcome=outcome,
        outcome_ok=outcome in s.expected,
        violations=violations + ([f"{len(finals)} final events"] if len(finals) > 1 else []),
        duplicate_notifications=max(0, notes - expected_notes),
        used_agent=run is not None,
        llm_calls=run.steps if run else 0,
        cost_usd=run.cost_usd if run else 0.0,
        latency_ms=latency_ms,
        guardrail_blocks=run.guardrail_blocks if run else 0,
        invalid_tool_calls=run.invalid_tool_calls if run else 0,
        reason=final.reason if isinstance(final, CaseEscalated) else "",
        expected=list(s.expected),
    )


@dataclass(frozen=True)
class Summary:
    scenarios: int
    route_accuracy: float
    outcome_accuracy: float
    policy_violations: int
    duplicate_notifications: int
    exceptions: int
    handled_without_llm: int
    llm_calls: int
    guardrail_blocks: int
    invalid_tool_calls: int
    escalation_rate: float
    cost_usd: float
    latency_p50_ms: float
    latency_p95_ms: float


def summarise(results: Sequence[ScenarioResult]) -> Summary:
    n = len(results)
    exceptions = [r for r in results if r.outcome != "none"]
    latencies = sorted(r.latency_ms for r in results)
    p95_index = max(0, round(0.95 * len(latencies)) - 1)
    return Summary(
        scenarios=n,
        route_accuracy=sum(r.route_ok for r in results) / n,
        outcome_accuracy=sum(r.outcome_ok for r in results) / n,
        policy_violations=sum(len(r.violations) for r in results),
        duplicate_notifications=sum(r.duplicate_notifications for r in results),
        exceptions=len(exceptions),
        handled_without_llm=sum(1 for r in exceptions if not r.used_agent),
        llm_calls=sum(r.llm_calls for r in results),
        guardrail_blocks=sum(r.guardrail_blocks for r in results),
        invalid_tool_calls=sum(r.invalid_tool_calls for r in results),
        escalation_rate=(
            sum(r.outcome == "escalated" for r in exceptions) / len(exceptions) if exceptions else 0
        ),
        cost_usd=round(sum(r.cost_usd for r in results), 6),
        latency_p50_ms=round(statistics.median(latencies), 1) if latencies else 0.0,
        latency_p95_ms=round(latencies[p95_index], 1) if latencies else 0.0,
    )


def render_markdown(title: str, runs: dict[str, list[ScenarioResult]], note: str) -> str:
    lines = [f"# {title}", "", note, "", "## Summary", ""]
    summaries = {name: summarise(results) for name, results in runs.items()}
    header = "| Metric | " + " | ".join(summaries) + " |"
    lines += [header, "|---|" + "---:|" * len(summaries)]
    for key in Summary.__dataclass_fields__:
        cells = []
        for s in summaries.values():
            value = getattr(s, key)
            cells.append(f"{value:.3f}" if isinstance(value, float) else str(value))
        lines.append(f"| {key} | " + " | ".join(cells) + " |")
    for name, results in runs.items():
        lines += [
            "",
            f"## Scenarios — {name}",
            "",
            "| id | route | outcome | ok | LLM calls | blocks | invalid | notes |",
            "|---|---|---|:-:|---:|---:|---:|---|",
        ]
        for r in results:
            notes = "; ".join(r.violations) or r.reason
            lines.append(
                f"| {r.id} | {r.route} | {r.outcome} | {'✓' if r.outcome_ok else '✗'} | "
                f"{r.llm_calls} | {r.guardrail_blocks} | {r.invalid_tool_calls} | "
                f"{notes.replace('|', '/')[:90]} |"
            )
    return "\n".join(lines) + "\n"


def results_json(runs: dict[str, list[ScenarioResult]]) -> str:
    return json.dumps(
        {
            name: {
                "summary": summarise(results).__dict__,
                "scenarios": [r.__dict__ for r in results],
            }
            for name, results in runs.items()
        },
        indent=2,
    )
