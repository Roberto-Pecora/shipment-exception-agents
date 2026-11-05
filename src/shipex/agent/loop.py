"""Bounded tool-calling agent for exceptions the router could not settle.

Every run ends in exactly one of two events — `CaseResolved` or `CaseEscalated` — never
an exception and never silence. Budgets (steps, cost, wall time, invalid calls) and an
LLM outage all degrade to escalation, so the worst case is "a human looks at it", not a
stuck partition or a runaway bill.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import ValidationError

from shipex import telemetry
from shipex.agent import prompts
from shipex.agent.carrier import CarrierClient, CarrierError
from shipex.agent.guardrails import CaseState, GuardrailPolicy, check
from shipex.agent.llm import ChatModel, ChatReply, LLMError, Message, ToolCall
from shipex.agent.tools import (
    TOOLS,
    ClaimArgs,
    CustomsDocsArgs,
    EscalateArgs,
    RebookArgs,
    ResolveArgs,
    ToolArgs,
    ToolKind,
    tool_schemas,
)
from shipex.contracts.events import (
    Action,
    CaseEscalated,
    CaseResolved,
    ExceptionRouted,
    ResolutionCode,
    RunStats,
)


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 8
    max_cost_usd: float = 0.05
    max_wall_s: float = 90.0
    max_invalid_calls: int = 3


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    arguments: str
    outcome: Literal["ok", "invalid", "blocked", "error"]
    detail: str = ""


@dataclass(frozen=True)
class AgentOutcome:
    status: Literal["resolved", "escalated"]
    reason: str
    resolution_code: ResolutionCode | None
    customer_message: str | None
    actions: tuple[Action, ...]
    stats: RunStats
    transcript: tuple[ToolCallRecord, ...]

    def to_event(self, case: ExceptionRouted) -> CaseResolved | CaseEscalated:
        if self.status == "resolved":
            assert self.resolution_code is not None and self.customer_message is not None
            return CaseResolved(
                case_id=case.case_id,
                shipment_id=case.shipment.shipment_id,
                resolved_by="agent",
                resolution_code=self.resolution_code,
                actions=self.actions,
                customer_message=self.customer_message,
                run=self.stats,
            )
        return CaseEscalated(
            case_id=case.case_id,
            shipment_id=case.shipment.shipment_id,
            source="agent",
            reason=self.reason,
            actions=self.actions,
            run=self.stats,
        )


@dataclass
class _Run:
    case: ExceptionRouted
    state: CaseState
    model: str
    started: float
    clock: Callable[[], float]
    steps: int = 0
    tool_calls: int = 0
    invalid: int = 0
    blocks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    transcript: list[ToolCallRecord] = field(default_factory=list)

    def elapsed(self) -> float:
        return self.clock() - self.started

    def stats(self) -> RunStats:
        return RunStats(
            model=self.model,
            steps=self.steps,
            tool_calls=self.tool_calls,
            invalid_tool_calls=self.invalid,
            guardrail_blocks=self.blocks,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=round(self.cost_usd, 6),
            latency_ms=round(self.elapsed() * 1000, 1),
        )

    def finish(
        self,
        status: Literal["resolved", "escalated"],
        reason: str,
        code: ResolutionCode | None = None,
        message: str | None = None,
    ) -> AgentOutcome:
        return AgentOutcome(
            status=status,
            reason=reason,
            resolution_code=code,
            customer_message=message,
            actions=tuple(self.state.actions),
            stats=self.stats(),
            transcript=tuple(self.transcript),
        )

    def escalate(self, reason: str) -> AgentOutcome:
        return self.finish("escalated", reason)


class ShipmentExceptionAgent:
    def __init__(
        self,
        llm: ChatModel,
        carrier: CarrierClient,
        budget: AgentBudget | None = None,
        policy: GuardrailPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._llm = llm
        self._carrier = carrier
        self.carrier = carrier  # shared with the playbook worker in the in-process pipeline
        self._budget = budget or AgentBudget()
        self._policy = policy or GuardrailPolicy()
        self._clock = clock
        self._tools = tool_schemas()

    def run(self, case: ExceptionRouted) -> AgentOutcome:
        state = CaseState(
            case_id=case.case_id,
            shipment_id=case.shipment.shipment_id,
            declared_value_gbp=case.shipment.declared_value_gbp,
            service_level=case.shipment.service_level,
        )
        run = _Run(case, state, self._llm.model, self._clock(), self._clock)
        with telemetry.tracer.start_as_current_span(
            "agent.run",
            attributes={
                "shipex.case_id": case.case_id,
                "shipex.shipment_id": case.shipment.shipment_id,
                "shipex.status_code": case.shipment.status_code.value,
            },
        ) as span:
            outcome = self._loop(run)
            span.set_attribute("shipex.agent.status", outcome.status)
            span.set_attribute("shipex.agent.reason", outcome.reason)
            span.set_attribute("shipex.agent.steps", outcome.stats.steps)
            span.set_attribute("shipex.agent.cost_usd", outcome.stats.cost_usd)
        telemetry.AGENT_OUTCOMES.labels(outcome.status).inc()
        return outcome

    def _loop(self, run: _Run) -> AgentOutcome:
        budget = self._budget
        messages: list[Message] = [
            {"role": "system", "content": prompts.system_prompt(self._policy)},
            {"role": "user", "content": prompts.case_message(run.case)},
        ]
        while True:
            if run.steps >= budget.max_steps:
                return run.escalate(f"budget:steps exhausted after {run.steps} model calls")
            if run.elapsed() >= budget.max_wall_s:
                return run.escalate(f"budget:time exceeded {budget.max_wall_s:.0f}s")
            if run.cost_usd >= budget.max_cost_usd:
                return run.escalate(f"budget:cost reached ${run.cost_usd:.4f}")

            try:
                reply = self._chat(messages, run)
            except LLMError as exc:
                return run.escalate(f"llm_unavailable: {exc}")
            messages.append(reply.as_message())

            if not reply.tool_calls:
                run.invalid += 1
                if run.invalid > budget.max_invalid_calls:
                    return run.escalate("protocol: too many replies without a valid tool call")
                messages.append({"role": "user", "content": prompts.NUDGE})
                continue

            for call in reply.tool_calls:
                content, terminal = self._dispatch(call, run)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                if terminal is not None:
                    return terminal
                if run.invalid > budget.max_invalid_calls:
                    return run.escalate("protocol: too many invalid tool calls")

    def _chat(self, messages: list[Message], run: _Run) -> ChatReply:
        with telemetry.tracer.start_as_current_span(
            f"chat {self._llm.model}",
            attributes={"gen_ai.operation.name": "chat", "gen_ai.request.model": self._llm.model},
        ) as span:
            # The wall-clock budget is a hard deadline: the call gets only the time left.
            remaining = self._budget.max_wall_s - run.elapsed()
            reply = self._llm.chat(messages, self._tools, timeout_s=remaining)
            u = reply.usage
            span.set_attribute("gen_ai.usage.input_tokens", u.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", u.completion_tokens)
            span.set_attribute("shipex.llm.cost_usd", u.cost_usd)
        run.steps += 1
        run.prompt_tokens += u.prompt_tokens
        run.completion_tokens += u.completion_tokens
        run.cost_usd += u.cost_usd
        telemetry.LLM_TOKENS.labels(self._llm.model, "input").inc(u.prompt_tokens)
        telemetry.LLM_TOKENS.labels(self._llm.model, "output").inc(u.completion_tokens)
        telemetry.LLM_COST_USD.labels(self._llm.model).inc(u.cost_usd)
        return reply

    def _dispatch(self, call: ToolCall, run: _Run) -> tuple[str, AgentOutcome | None]:
        run.tool_calls += 1
        spec = TOOLS.get(call.name)
        if spec is None:
            run.invalid += 1
            run.transcript.append(ToolCallRecord(call.name, call.arguments, "invalid", "unknown"))
            return _dumps({"error": f"unknown tool {call.name!r}"}), None
        try:
            args = spec.args.model_validate_json(call.arguments)
        except ValidationError as exc:
            run.invalid += 1
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()
            )
            run.transcript.append(ToolCallRecord(call.name, call.arguments, "invalid", detail))
            return _dumps({"error": f"invalid arguments: {detail}"}), None

        blocked = check(args, run.state, self._policy)
        if blocked:
            run.blocks += 1
            telemetry.GUARDRAIL_BLOCKS.labels(call.name).inc()
            run.transcript.append(ToolCallRecord(call.name, call.arguments, "blocked", blocked))
            return _dumps({"blocked": blocked}), None

        if spec.kind is ToolKind.TERMINAL:
            run.transcript.append(ToolCallRecord(call.name, call.arguments, "ok"))
            if isinstance(args, ResolveArgs):
                code = ResolutionCode(args.resolution_code)
                return "", run.finish("resolved", "resolved", code, args.customer_message)
            assert isinstance(args, EscalateArgs)
            return "", run.escalate(f"agent_requested: {args.reason}")

        with telemetry.tracer.start_as_current_span(
            f"execute_tool {call.name}", attributes={"gen_ai.tool.name": call.name}
        ):
            try:
                result = self._execute(call.name, args, run.state)
            except CarrierError as exc:
                run.transcript.append(ToolCallRecord(call.name, call.arguments, "error", str(exc)))
                hint = "temporary; you may retry once or escalate" if exc.transient else "permanent"
                return _dumps({"error": str(exc), "kind": hint}), None
        run.transcript.append(ToolCallRecord(call.name, call.arguments, "ok"))
        return _dumps(result), None

    def _execute(self, name: str, args: ToolArgs, state: CaseState) -> dict[str, Any]:
        sid = state.shipment_id
        if name == "get_tracking":
            return self._carrier.get_tracking(sid).model_dump(mode="json")
        if name == "get_rebooking_options":
            options = self._carrier.get_rebooking_options(sid)
            state.offered_options.update({o.option_id: o for o in options})
            return {"options": [o.model_dump(mode="json") for o in options]}
        if isinstance(args, RebookArgs):
            option = state.offered_options[args.option_id]
            conf = self._carrier.rebook(sid, args.option_id, f"{state.case_id}:rebook")
            state.actions.append(
                Action(
                    kind="rebook",
                    params={
                        "option_id": option.option_id,
                        "service": option.service,
                        "cost_delta_gbp": option.cost_delta_gbp,
                        "new_eta": conf.new_eta.isoformat(),
                    },
                    external_ref=conf.rebooking_id,
                )
            )
            return conf.model_dump(mode="json")
        if isinstance(args, CustomsDocsArgs):
            req = self._carrier.request_customs_documents(
                sid, args.documents, f"{state.case_id}:customs_docs"
            )
            state.actions.append(
                Action(
                    kind="customs_docs_request",
                    params={"documents": req.documents},
                    external_ref=req.request_id,
                )
            )
            return req.model_dump(mode="json")
        if isinstance(args, ClaimArgs):
            claim = self._carrier.open_claim(sid, args.amount_gbp, f"{state.case_id}:claim")
            state.actions.append(
                Action(
                    kind="claim",
                    params={"amount_gbp": claim.amount_gbp},
                    external_ref=claim.claim_id,
                )
            )
            return claim.model_dump(mode="json")
        raise AssertionError(f"no executor for {name}")


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, default=str)
