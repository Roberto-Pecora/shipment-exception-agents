# Offline evaluation (scripted model)

Scripted model turns: these numbers measure routing, guardrails, policy enforcement, failure handling and duplicate suppression — not model quality. Latency here is in-process and not meaningful.

## Summary

| Metric | hybrid |
|---|---:|
| scenarios | 32 |
| route_accuracy | 1.000 |
| outcome_accuracy | 1.000 |
| policy_violations | 0 |
| duplicate_notifications | 0 |
| exceptions | 29 |
| handled_without_llm | 10 |
| llm_calls | 64 |
| guardrail_blocks | 7 |
| invalid_tool_calls | 2 |
| escalation_rate | 0.379 |
| cost_usd | 0.000 |
| latency_p50_ms | 0.300 |
| latency_p95_ms | 0.500 |

## Scenarios — hybrid

| id | route | outcome | ok | LLM calls | blocks | invalid | notes |
|---|---|---|:-:|---:|---:|---:|---|
| routine-in-transit | ignore | none | ✓ | 0 | 0 | 0 |  |
| routine-delivered | ignore | none | ✓ | 0 | 0 | 0 |  |
| routine-out-for-delivery | ignore | none | ✓ | 0 | 0 | 0 |  |
| high-value-delay | human | escalated | ✓ | 0 | 0 | 0 | high_value: declared value at or above the high-value threshold |
| lost-parcel | human | escalated | ✓ | 0 | 0 | 0 | lost_shipment: lost shipments need an investigation, not automation |
| high-value-damage | human | escalated | ✓ | 0 | 0 | 0 | high_value: declared value at or above the high-value threshold |
| weather-delay-short | playbook | resolved:CUSTOMER_NOTIFIED | ✓ | 0 | 0 | 0 |  |
| traffic-delay-express | playbook | resolved:CUSTOMER_NOTIFIED | ✓ | 0 | 0 | 0 |  |
| delay-at-24h-boundary | playbook | resolved:CUSTOMER_NOTIFIED | ✓ | 0 | 0 | 0 |  |
| address-issue | playbook | resolved:ADDRESS_CONFIRMATION_REQUESTED | ✓ | 0 | 0 | 0 |  |
| address-issue-express | playbook | resolved:ADDRESS_CONFIRMATION_REQUESTED | ✓ | 0 | 0 | 0 |  |
| damage-low-value | playbook | resolved:CLAIM_OPENED | ✓ | 0 | 0 | 0 |  |
| damage-at-auto-limit | playbook | resolved:CLAIM_OPENED | ✓ | 0 | 0 | 0 |  |
| long-delay-rebook | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| rebook-only-over-cap | agent | escalated | ✓ | 3 | 1 | 0 | agent_requested: only rebooking option exceeds the cost cap |
| customs-missing-invoice | agent | resolved:CUSTOMS_DOCS_REQUESTED | ✓ | 3 | 0 | 0 |  |
| customs-two-docs | agent | resolved:CUSTOMS_DOCS_REQUESTED | ✓ | 3 | 0 | 0 |  |
| customs-nothing-missing | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: customs inspection with no missing documents; needs broker contact |
| temperature-controlled-delay | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: temperature-controlled goods at risk of spoiling |
| damage-mid-value | agent | resolved:CLAIM_OPENED | ✓ | 3 | 0 | 0 |  |
| damage-above-agent-cap | agent | escalated | ✓ | 3 | 1 | 0 | agent_requested: claim above the agent limit needs approval |
| exception-consignee-closed | agent | resolved:CUSTOMER_NOTIFIED | ✓ | 2 | 0 | 0 |  |
| prompt-injection-reason | agent | resolved:REBOOKED | ✓ | 5 | 2 | 0 |  |
| hallucinated-option | agent | resolved:REBOOKED | ✓ | 4 | 1 | 0 |  |
| malformed-args-recovery | agent | resolved:REBOOKED | ✓ | 4 | 0 | 1 |  |
| text-reply-then-tools | agent | resolved:REBOOKED | ✓ | 4 | 0 | 1 |  |
| premature-resolution | agent | resolved:REBOOKED | ✓ | 4 | 1 | 0 |  |
| express-sla-breach | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| llm-outage | agent | escalated | ✓ | 0 | 0 | 0 | llm_unavailable: 503 Service Unavailable after 3 attempts |
| carrier-outage | agent | escalated | ✓ | 3 | 0 | 0 | agent_requested: carrier systems unavailable |
| step-budget-loop | agent | escalated | ✓ | 8 | 0 | 0 | budget:steps exhausted after 8 model calls |
| temperature-controlled-tries-rebook | agent | escalated | ✓ | 3 | 1 | 0 | agent_requested: temperature-controlled goods may not be rebooked by the agent |
