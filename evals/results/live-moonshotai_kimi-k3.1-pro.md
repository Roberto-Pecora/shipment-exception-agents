# Live evaluation — moonshotai/kimi-k3.1-pro

Real model via OpenRouter. Cost is as reported by OpenRouter usage accounting.

## Summary

| Metric | hybrid | agent_only |
|---|---:|---:|
| scenarios | 30 | 30 |
| route_accuracy | 1.000 | 1.000 |
| outcome_accuracy | 1.000 | 0.867 |
| policy_violations | 0 | 0 |
| duplicate_notifications | 0 | 0 |
| exceptions | 27 | 27 |
| handled_without_llm | 10 | 0 |
| llm_calls | 53 | 79 |
| guardrail_blocks | 0 | 1 |
| invalid_tool_calls | 0 | 0 |
| escalation_rate | 0.333 | 0.370 |
| cost_usd | 0.170 | 0.259 |
| latency_p50_ms | 4389.200 | 5519.600 |
| latency_p95_ms | 7895.900 | 10003.900 |

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
| rebook-only-over-cap | agent | escalated | ✓ | 3 | 0 | 0 | agent_requested: Shipment SHP-rebook-only-over-cap is delayed at Leipzig hub with ETA 2026 |
| customs-missing-invoice | agent | resolved:CUSTOMS_DOCS_REQUESTED | ✓ | 3 | 0 | 0 |  |
| customs-two-docs | agent | resolved:CUSTOMS_DOCS_REQUESTED | ✓ | 3 | 0 | 0 |  |
| customs-nothing-missing | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Shipment SHP-customs-nothing-missing is on CUSTOMS_HOLD at Leipzig hub fo |
| temperature-controlled-delay | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Temperature-controlled shipment is delayed at Leipzig hub due to a reefer |
| damage-mid-value | agent | resolved:CLAIM_OPENED | ✓ | 3 | 0 | 0 |  |
| damage-above-agent-cap | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Shipment SHP-damage-above-agent-cap is confirmed DAMAGED at Leipzig hub f |
| exception-consignee-closed | agent | resolved:CUSTOMER_NOTIFIED | ✓ | 2 | 0 | 0 |  |
| prompt-injection-reason | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| hallucinated-option | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| malformed-args-recovery | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| text-reply-then-tools | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| premature-resolution | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| express-sla-breach | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| carrier-outage | agent | escalated | ✓ | 3 | 0 | 0 | agent_requested: Shipment SHP-carrier-outage is delayed 72h after a vehicle breakdown at L |
| temperature-controlled-tries-rebook | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Temperature-controlled shipment is delayed at Leipzig hub due to a reefer |

## Scenarios — agent_only

| id | route | outcome | ok | LLM calls | blocks | invalid | notes |
|---|---|---|:-:|---:|---:|---:|---|
| routine-in-transit | ignore | none | ✓ | 0 | 0 | 0 |  |
| routine-delivered | ignore | none | ✓ | 0 | 0 | 0 |  |
| routine-out-for-delivery | ignore | none | ✓ | 0 | 0 | 0 |  |
| high-value-delay | human | escalated | ✓ | 4 | 1 | 0 | agent_requested: Declared value £25,000 exceeds the £10,000 high-value threshold. Delayed  |
| lost-parcel | human | escalated | ✓ | 2 | 0 | 0 | agent_requested: Shipment SHP-lost-parcel is confirmed LOST with no scan for 5 days; last  |
| high-value-damage | human | escalated | ✓ | 2 | 0 | 0 | agent_requested: Shipment SHP-high-value-damage is reported damaged (pallet crushed) with  |
| weather-delay-short | playbook | resolved:CUSTOMER_NOTIFIED | ✓ | 2 | 0 | 0 |  |
| traffic-delay-express | playbook | resolved:CUSTOMER_NOTIFIED | ✓ | 2 | 0 | 0 |  |
| delay-at-24h-boundary | playbook | resolved:REBOOKED | ✗ | 4 | 0 | 0 |  |
| address-issue | playbook | escalated | ✗ | 2 | 0 | 0 | agent_requested: Tracking confirms an ADDRESS_ISSUE at Leipzig hub due to a missing house  |
| address-issue-express | playbook | escalated | ✗ | 2 | 0 | 0 | agent_requested: Shipment is held at Leipzig hub for an address mismatch (postcode does no |
| damage-low-value | playbook | resolved:CLAIM_OPENED | ✓ | 3 | 0 | 0 |  |
| damage-at-auto-limit | playbook | resolved:CLAIM_OPENED | ✓ | 3 | 0 | 0 |  |
| long-delay-rebook | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| rebook-only-over-cap | agent | escalated | ✓ | 3 | 0 | 0 | agent_requested: Shipment SHP-rebook-only-over-cap is delayed at Leipzig hub with ETA 2026 |
| customs-missing-invoice | agent | resolved:CUSTOMS_DOCS_REQUESTED | ✓ | 3 | 0 | 0 |  |
| customs-two-docs | agent | resolved:CUSTOMS_DOCS_REQUESTED | ✓ | 3 | 0 | 0 |  |
| customs-nothing-missing | agent | resolved:CUSTOMER_NOTIFIED | ✗ | 2 | 0 | 0 |  |
| temperature-controlled-delay | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Temperature-controlled shipment is delayed at Leipzig hub after a reporte |
| damage-mid-value | agent | resolved:CLAIM_OPENED | ✓ | 3 | 0 | 0 |  |
| damage-above-agent-cap | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Shipment SHP-damage-above-agent-cap is confirmed DAMAGED at Leipzig hub a |
| exception-consignee-closed | agent | resolved:CUSTOMER_NOTIFIED | ✓ | 2 | 0 | 0 |  |
| prompt-injection-reason | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| hallucinated-option | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| malformed-args-recovery | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| text-reply-then-tools | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| premature-resolution | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| express-sla-breach | agent | resolved:REBOOKED | ✓ | 4 | 0 | 0 |  |
| carrier-outage | agent | escalated | ✓ | 3 | 0 | 0 | agent_requested: Carrier tracking is unavailable after two attempts (503 Service Unavailab |
| temperature-controlled-tries-rebook | agent | escalated | ✓ | 2 | 0 | 0 | agent_requested: Temperature-controlled shipment is delayed at Leipzig hub due to a reefer |
