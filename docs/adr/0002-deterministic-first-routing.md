# ADR 0002 — Deterministic-first routing

**Status:** accepted

## Context
Most carrier exceptions are routine: a short weather delay that still meets the SLA, an
address problem, minor damage. Sending these to an LLM adds latency, cost and variance for
no gain. Some exceptions must never be automated (high value, lost shipments). Only a
minority genuinely need investigation across tools.

## Decision
An ordered policy table (`src/shipex/routing/rules.py`), first match wins:

| Rule | Route |
|---|---|
| routine scan | ignore |
| declared value ≥ high-value threshold | human |
| lost shipment | human |
| delay ≤ 24h, not temperature-controlled, SLA still met | playbook `re_eta_notify` |
| address issue | playbook `address_confirmation` |
| damage ≤ auto-claim limit | playbook `auto_claim` |
| anything else | agent |

Every decision carries a `rule_id`, emitted as a span attribute and a Prometheus label, so
the route mix is observable and each rule is unit-tested in a table.

## Consequences
- In the scenario suite, 10 of 29 exceptions never touch the model, and every
  high-risk case reaches a human regardless of model behaviour.
- Rules encode policy explicitly; changing a threshold is a reviewed config change, not a
  prompt edit. The agent-only baseline (`--strategy agent_only`) exists to measure what
  this buys on a real model.
- New exception types default to the agent. Promoting a pattern into a rule is the
  intended path as volume reveals it.
- The router also enforces one open automated case per shipment (`open_cases`). A second
  exception while a case is open becomes `concurrent_case` and goes to a human, so a
  playbook and the agent never act on the same parcel at once.
