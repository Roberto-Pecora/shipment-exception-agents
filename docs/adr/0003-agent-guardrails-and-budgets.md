# ADR 0003 — Agent guardrails and budgets

**Status:** accepted

## Context
The agent can take irreversible, costly actions (rebooking freight, opening claims) and it
reads untrusted carrier free text. Prompt instructions alone are not a control.

## Decision
- **Hand-written loop, not a framework.** One small module (`agent/loop.py`); owning it
  makes every failure path explicit and testable.
- **Tool schema = validation.** Each tool's arguments are a pydantic model that produces the
  JSON schema sent to the model and validates the reply. Invalid arguments go back to the
  model as a tool error and are counted.
- **Deterministic guardrails before execution** (`agent/guardrails.py`): tools act only on
  the case's shipment; rebook only an *offered* option under the cost cap, once; claims
  capped by declared value and an agent limit; customer messages may not promise refunds,
  compensation or guarantees; a resolution code is accepted only if backed by the matching
  successful action; temperature-controlled goods may only be escalated. Blocked calls
  are returned to the model with the reason. The last rule moved from the prompt into code
  after a smaller model ignored the prompt version in a live run.
- **Budgets**: max model calls, max spend, max wall time, max invalid calls. A breach, an
  LLM outage or an explicit `escalate_to_human` all end the run as `CaseEscalated`.
  Every run ends in exactly one event — never an exception, never silence.

## Consequences
- The offline eval re-derives policy violations from emitted events independently of the
  guardrail code; a test disables the guardrails and asserts the eval catches it.
- Escalation is the universal fallback, so a degraded model or provider increases ops
  workload rather than causing wrong actions. The escalation rate is a monitored metric.
