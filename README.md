# shipment-exception-agents

Event-driven handling of carrier shipment exceptions (delays, customs holds, damage, address
problems) built as microservices on Kafka. A deterministic router settles everything a
rule can settle; only the ambiguous remainder reaches a bounded, tool-calling LLM agent,
whose actions are checked by code-level guardrails before they touch the carrier.

The point is not the agent; it is what surrounds it to make it safe to run: idempotent
at-least-once delivery, retries and dead-letter queues, idempotency keys on every side
effect, budgets that degrade to human escalation, one OpenTelemetry trace across Python
and TypeScript services, and an evaluation gate in CI.

> **Thesis:** in production, an agent is one component in an event-driven system. Use a
> deterministic pipeline wherever a rule can decide, and make the agent's failure mode
> "a human looks at it", never "a wrong action" or "a stuck queue".

```text
carrier webhook ─► ingest-api (FastAPI) ─► shipment.events.v1 ─► router (rules, first match wins)
                   validate · dedupe                                │
                          ┌─────────────── ignore / human ◄─────────┤
                          │        exceptions.playbook.v1 ◄─────────┤──────► exceptions.agent.v1
                          │                 ▼                                      ▼
                          │        playbook worker                        agent worker (bounded loop)
                          │        (pure functions)                       guardrails · budgets
                          │                 │                                      │ HTTP + Idempotency-Key
                          │                 │                          carrier-gateway (TypeScript/Fastify)
                          ▼                 ▼                                      ▼
                    escalations.v1 ◄────────┴──── resolutions.v1 ◄─────────────────┘
                                         ▼
                                 notifier ─► notification outbox (Postgres)
                                    │ resolved: close case · escalated: hold for ops
                                    ▼
                                 ops-api ── a person releases held cases

every consumer: commit after publish · idempotent by event id · retries → <topic>.dlq
traceparent in Kafka headers → one trace in Jaeger · Prometheus metrics per service
```

## Why deterministic-first?

Most exceptions are routine. A 6-hour weather delay that still meets the SLA needs a
re-ETA and a message, not a model call. A £25k consignment or a lost parcel should reach a
person whatever a model thinks. The router (`src/shipex/routing/rules.py`) is an ordered
policy table with a `rule_id` on every decision; the agent gets only what no rule can
settle. In the scenario suite, **10 of 29 exceptions never touch the model**, and all
high-risk cases go to a human by rule (ADR 0002).

## Services

| Service | Language | Role |
|---|---|---|
| `ingest-api` | Python / FastAPI | Carrier webhook. Validates against a strict contract, dedupes on the carrier's event id and content, rejects conflicting corrections (409), publishes. |
| `router` | Python | Policy table → ignore / playbook / agent / human. |
| `playbook` | Python | Deterministic pipelines: re-ETA notify, address confirmation, auto-claim (filed with the carrier-gateway). |
| `agent` | Python | Bounded tool-calling loop via OpenRouter; guardrails and budgets; always emits resolved or escalated. |
| `carrier-gateway` | TypeScript / Fastify + zod | Tracking, rebooking options, rebook, customs-document requests, claims. Idempotency keys enforced; fault injection. |
| `notifier` | Python | Writes customer messages and ops tickets to an idempotent outbox; closes resolved cases and holds escalated ones for ops. |
| `ops-api` | Python / FastAPI | Shows a shipment's open or held case and lets a person release a held case back to automation. |

Infra: Redpanda (Kafka API), Postgres, Jaeger, Prometheus, all in `docker-compose.yml`.

## Reliability

- **Delivery** (ADR 0001): offsets are committed only after outputs are flushed. Output ids
  are derived from input ids (`uuid5`), so a crash-replay re-emits identical events that
  downstream consumers dedupe. Transient errors retry with jittered backoff, then go to
  `<topic>.dlq` with the error and attempt count; the partition keeps flowing.
- **Side effects**: the agent rebooks with `Idempotency-Key: <case_id>:rebook`; the gateway
  replays the original response for a repeated key and rejects a key reused with a
  different body.
- **Outbound calls**: timeouts, Retry-After-aware backoff and a circuit breaker on both the
  LLM provider and the carrier-gateway.

## The agent

- Tools: `get_tracking`, `get_rebooking_options`, `rebook_shipment`,
  `request_customs_documents`, `open_damage_claim`, and two terminal tools,
  `resolve_case` / `escalate_to_human`. Argument schemas are pydantic models: the same
  class generates the JSON schema and validates the reply.
- Guardrails (ADR 0003), enforced in code before execution: act only on this case's
  shipment; rebook only an offered option, under the cost cap, once; claims capped by
  declared value and an agent limit; no refunds, compensation or guarantees in customer
  messages; a resolution code needs the matching successful action; temperature-controlled
  and high-value (declared value ≥ £10,000) shipments can only be escalated.
- Budgets: model calls, spend, wall time, invalid calls. A breach, a provider outage, or
  the model's own choice ends the run as `CaseEscalated`.
- Carrier free text is treated as data: a prompt-injection scenario tries to redirect a
  rebooking to another shipment and promise a refund; both calls are blocked.

## Evaluation

`evals/scenarios.jsonl` holds 32 scenarios (routine scans, human-only cases, playbook
cases, agent cases, and failure modes: malformed tool arguments, hallucinated option ids,
premature resolution, prompt injection, carrier outage, LLM outage, step-budget loops).
Every scenario is delivered **twice** to check duplicate suppression.

### Offline (scripted model): CI gate

Each agent scenario carries scripted model turns, so these numbers measure the harness
(routing, guardrails, policy enforcement, failure handling, duplicates), **not model quality**.
Policy violations are re-derived from the emitted events, independently of the guardrail
code; `tests/test_evaluation.py` switches the guardrails off and checks that the eval
catches it.

| Metric | Value |
|---|---:|
| Scenarios | 32 |
| Routing accuracy | 1.000 |
| Outcome accuracy | 1.000 |
| Policy violations | 0 |
| Duplicate notifications (32 × 2 deliveries) | 0 |
| Exceptions handled without the model | 10 / 29 |
| Guardrail blocks (all intended) | 7 |
| Invalid tool calls recovered | 2 |

Full table: [`evals/results/offline.md`](evals/results/offline.md).

### Failure drills

`evals/failure_suite.py` exercises the delivery layer:

| Drill | Result |
|---|:-:|
| Poison message isolated to DLQ; healthy case behind it still notified | pass |
| Agent worker killed after rebooking, before committing: replay does not rebook twice | pass |
| Transient sink failure retried, no DLQ | pass |
| Persistent sink failure: DLQ after 2 attempts, later case on the partition still notified | pass |
| 100 deliveries of 20 events → 20 notifications | pass |

### Live (real model)

`make eval-live` runs the same scenarios (minus the scripted-only failure cases) against a
real model, comparing the hybrid router with an agent-only baseline on outcome accuracy,
policy violations, model calls, cost and latency.

This run predates eight hardening changes made after a design review (hard model-call
deadline, stored agent outcomes, a write-ahead action journal, one case per shipment,
escalated cases held for ops, a high-value guardrail, ingest conflict detection, claims
filed through the gateway); those are verified by tests and the offline gate, not by a
repeat live run.

One model, `moonshotai/kimi-k3.1-pro` via OpenRouter: 30 scenarios (the two scripted-only
failure cases are offline-only), one sample each, temperature 0, per-case budget $0.15
(full table: [`evals/results/live-moonshotai_kimi-k3.1-pro.md`](evals/results/live-moonshotai_kimi-k3.1-pro.md)).

| Metric | Hybrid (router + agent) | Agent only |
|---|---:|---:|
| Outcome accuracy | 1.000 (30/30) | 0.867 (26/30) |
| Policy violations | 0 | 0 |
| Duplicate notifications | 0 | 0 |
| Model calls | 53 | 77 |
| Cost (USD) | 0.161 | 0.242 |
| Latency p50 / p95 (s) | 3.5 / 9.4 | 5.7 / 14.9 |

How to read this:

- **The scenarios were corrected after a first run, so treat 1.000 with care.** The first
  run scored 0.897 / 0.724 ([`before-fix-kimi-k3.1-pro.md`](evals/results/before-fix-kimi-k3.1-pro.md)).
  Several misses were my fixture's fault: the tracking ETA the agent saw left out the
  reported delay, so SLA breaches looked fine and the model reasonably declined to rebook.
  Exploratory runs on two other models showed the same misses (those results are not kept).
  I fixed the fixture and reran. This is one sample on 30 scenarios I revised after seeing
  the failures: it shows this model and the harness agree on these cases, not a benchmark.
- **Agent-only misses.** It handled a £25,000 consignment with a customer notification
  instead of escalating. The hybrid router sends that to a human by rule; at the time the
  agent's guardrails did not cap declared value. They now do (high-value cases can only be
  escalated), which would block that action; the live run has not been repeated since.
  Two address-issue cases escalated because the agent
  has no address-confirmation tool, which my expected outcomes arguably should allow, and a
  24-hour delay was rebooked, which is defensible. The comparison favours the router by
  construction, since the scenarios were written around its rules.
- **The temperature-controlled guardrail never fired live** (0 blocks): Kimi escalated
  those cases unprompted. It is verified offline only, by unit tests and a scripted
  scenario. It exists because an exploratory run with a smaller model (`qwen3-30b-a3b`)
  rebooked temperature-controlled goods when only the prompt said not to.

## Quick start

```bash
make install            # Python venv (uv) + gateway node_modules
make ci                 # lint, mypy --strict, tests, eval gate, failure drills, gateway tests

cp .env.example .env    # optional: OPENROUTER_API_KEY, AGENT_MODEL
make up                 # Redpanda, Postgres, Jaeger, Prometheus, 7 services
make smoke              # posts every scenario twice, waits for exactly one notification each
```

Then open Jaeger at http://localhost:16686 (one trace per case, across services) and
Prometheus at http://localhost:9090 (`shipex_routes_total`, `shipex_agent_outcomes_total`,
`shipex_llm_cost_usd_total`, `shipex_dead_letters_total`). Without an API key the agent
worker runs with no model and agent cases escalate to ops: the system degrades and does
not stall. `FAULT_RATE=0.2 make up` makes the carrier-gateway fail 20% of requests.

## Development

```text
src/shipex/
  contracts/events.py   versioned envelope + payloads (deterministic ids)
  bus/                  Protocols, in-memory bus, Kafka adapters, ConsumerRunner
  routing/rules.py      policy table
  playbooks/            deterministic pipelines
  agent/                llm (OpenRouter + scripted), carrier client, tools, guardrails, loop
  handlers.py           pure Envelope -> [Outgoing] per service
  pipeline.py           whole graph in-process (tests, evals)
  services/             ingest_api (FastAPI), worker (router|playbook|agent|notifier)
services/carrier-gateway/   TypeScript service + vitest
evals/                  scenarios, runner, failure drills, results
docs/ARCHITECTURE.md    system map (services, events, delivery, agent, observability)
docs/adr/               architecture decisions
```

Tests are hermetic (no broker, database, key or network): 94 pytest tests and 10 vitest
tests. CI runs ruff, `mypy --strict`, pytest with coverage, the offline eval gate, the
failure drills, gateway typecheck/tests/build, and builds every image. The repo was built
with **Claude Code**; `CLAUDE.md` holds the rules agents and humans follow here, and
`.github/pull_request_template.md` holds the review checklist.

## Scope and limitations

- Carrier data is a fixture-backed gateway, not a real carrier integration.
- Offline results validate the harness, not the model; the live run is one sample of one
  model on a scenario set revised after a first run (above).
- `make smoke` has been run on the full docker-compose stack without an API key (every
  agent case escalated, as designed). Jaeger showed one trace per case across ingest →
  router → agent → notifier. The agent → carrier-gateway trace link only appears once a
  model makes tool calls, so it has not yet been seen in Jaeger.
- Retries are in-process, so a long retry blocks its partition (bounded; see ADR 0001).
- One Postgres serves the idempotency ledger and outbox for simplicity; in production each
  service would own its store.
- No auth on ingest-api or the gateway; the admin seeding endpoint is for local use only.

## License

MIT
