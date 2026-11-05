# Architecture

How `shipment-exception-agents` is put together, and why. The decisions behind the
non-obvious parts are in [`docs/adr/`](adr/); this document is the map.

## 1. Purpose and shape

Carriers send status events. Most are routine; a minority are exceptions (delay, customs
hold, damage, address problem, lost). Each exception must end in exactly one outcome:
a customer message, or an escalation to a human. Nothing may be lost, duplicated, or
resolved by an unchecked model action.

The system is a pipeline of small services connected by Kafka topics. A deterministic
router decides who handles each exception; an LLM agent handles only what rules cannot.

```text
carrier webhook
      │  POST /v1/shipment-events
      ▼
 ingest-api ──────────────► shipment.events.v1
 (validate, dedupe)                │
                                   ▼
                               router ── ignore (routine scan: no output)
                            (policy table)
                  ┌────────────────┼──────────────────────┐
                  │ playbook       │ agent                │ human
                  ▼                ▼                      │
     exceptions.playbook.v1   exceptions.agent.v1         │
                  │                │                      │
                  ▼                ▼                      │
          playbook worker     agent worker                │
          (pure functions)    (bounded LLM loop) ──HTTP──► carrier-gateway (TS)
                  │                │                      │
                  ▼                ▼                      ▼
            resolutions.v1 ◄───────┴──────► escalations.v1 ◄┘
                  └───────────┬────────────────┘
                              ▼
                          notifier ──► notification_outbox (Postgres)

Each topic has a `<topic>.dlq`. Every consumer: idempotent, commit-after-publish, retries.
```

## 2. Services

| Service | Process | Consumes | Produces | State |
|---|---|---|---|---|
| `ingest-api` | FastAPI (`services/ingest_api.py`) | HTTP | `shipment.events.v1` | `ingested_events` |
| `router` | worker (`services/worker.py router`) | `shipment.events.v1` | playbook queue, agent queue, `escalations.v1` | `processed_events`, `open_cases` |
| `playbook` | worker | `exceptions.playbook.v1` | `resolutions.v1` | `processed_events` |
| `agent` | worker | `exceptions.agent.v1` | `resolutions.v1`, `escalations.v1` | `processed_events`, `agent_outcomes` |
| `notifier` | worker | `resolutions.v1`, `escalations.v1` | none (writes outbox) | `notification_outbox`, `open_cases` |
| `carrier-gateway` | Node/Fastify (`services/carrier-gateway`) | HTTP from `agent` and `playbook` | none | in-memory (demo) |

The four workers are one Python image with the role passed as an argument; each runs in its
own consumer group (`shipex-<role>`), so each can be scaled and restarted independently.
The business logic of each worker is a pure function in `handlers.py`
(`Envelope -> [Outgoing]`); everything about delivery lives in `ConsumerRunner`.

## 3. Event model (`contracts/events.py`)

Every message is an `Envelope`: `event_id`, `event_type`, `schema_version`, `occurred_at`,
`correlation_id`, `causation_id`, `key`, and a typed `payload`. Payloads are frozen,
`extra="forbid"` pydantic models: `ShipmentEvent`, `ExceptionRouted`, `CaseResolved`,
`CaseEscalated`.

- **Key** is `shipment_id`, so events for one shipment stay ordered within a partition.
- **Ids are deterministic.** The ingest id is `uuid5(carrier:carrier_event_id)`; every
  derived event is `uuid5(parent_id:salt)`. A handler re-run after a crash therefore
  re-emits the *same* ids, which downstream consumers dedupe. Because the ingest id ignores
  content, ingest also stores a content fingerprint and rejects a re-used id with different
  content (409); corrections must arrive under a new `carrier_event_id`.
- **`case_id`** is the ingest event id. It follows the case through every event and is the
  base of every idempotency key sent to the carrier.
- **`correlation_id`** is constant across a case; `causation_id` points at the parent event.
- Contracts are versioned by topic name (`.v1`) and `schema_version`; they are never
  edited in place.

## 4. Delivery semantics (`bus/consumer.py`, ADR 0001)

`ConsumerRunner.process` handles one record:

```text
poll ─► parse envelope ──(poison)────────────────────────────► DLQ ─► commit
          │
          ▼
   seen(consumer, event_id)? ──yes──► commit ─► "duplicate"
          │ no
          ▼
   handler(envelope) ── PermanentError / ValidationError ─────► DLQ ─► commit
          │              TransientError / unexpected: retry with jittered
          │              exponential backoff, max_attempts (default 3), then DLQ
          ▼
   publish outputs ─► flush (raises if any delivery failed)
          ▼
   mark(consumer, event_id) ─► commit offset
```

What this guarantees:

- **At-least-once.** The offset is committed only after outputs are durable. A crash before
  commit replays the input.
- **Effectively-once per consumer.** The Postgres ledger skips ids already processed. The
  window between `flush` and `mark` can replay a record; because output ids are derived from
  input ids, the replayed outputs are duplicates by id and are absorbed downstream.
- **No poison blocking.** Poison records and exhausted retries go to `<topic>.dlq` with
  `x-error`, `x-attempts`, `x-consumer` and `x-source-offset` headers, then the offset is
  committed so the partition keeps flowing.
- **Side effects are outside this guarantee**, so they carry their own idempotency keys
  (section 7).
- **An agent case runs to completion at most once.** Replaying a record would re-run the
  agent, and a second run can pick a *different* action that no idempotency key catches.
  The first finished outcome is stored under `case_id` (`agent_outcomes`, first writer
  wins) and every redelivery republishes it without calling the model.

Kafka specifics (`bus/kafka.py`): idempotent producer with `acks=all`; consumer with
auto-commit off and explicit synchronous commits. The `Publisher` and `Subscriber`
Protocols let the same runner run on an `InMemoryBus` in tests and evals.

## 5. Routing (`routing/rules.py`, ADR 0002)

An ordered policy table; first match wins; every decision has a `rule_id`.

| # | Rule id | Condition | Route |
|---|---|---|---|
| 1 | `routine_scan` | picked up / in transit / out for delivery / delivered | ignore |
| 2 | `high_value` | declared value ≥ £10,000 | human |
| 3 | `lost_shipment` | status LOST | human |
| 4 | `delay_within_sla` | DELAYED, ≤ 24h, not temperature-controlled, ETA + delay still within SLA | playbook `re_eta_notify` |
| 5 | `address_issue` | status ADDRESS_ISSUE | playbook `address_confirmation` |
| 6 | `low_value_damage` | DAMAGED and value ≤ £500 | playbook `auto_claim` |
| – | `needs_judgement` | anything else (fallback) | agent |

Thresholds come from config (`HIGH_VALUE_GBP`, `AUTO_CLAIM_LIMIT_GBP`,
`MAX_PLAYBOOK_DELAY_HOURS`). A delay with an unknown ETA cannot be proven to meet the SLA,
so it falls through to the agent. Playbooks (`playbooks/`) are pure functions of the routed
case and cost nothing per run.

**One automated case per shipment.** Before routing an exception to a playbook, the agent or
a human, the router opens a case for the shipment in `open_cases`; the notifier closes it
once the case's final event is written. If a case is already open (a DELAYED case still with
the agent when a DAMAGED event arrives), the new exception is routed to a human with
`rule_id=concurrent_case` instead of starting a second case that could act in parallel. The
router consumes each shipment's events in order, so this check needs no locking.

## 6. The agent (`agent/`, ADR 0003)

```text
ExceptionRouted ─► ShipmentExceptionAgent.run
                      │
        ┌─────────────▼──────────────┐
        │ loop: budgets ok? ─no─► escalate("budget:…")
        │       model.chat(messages, tools)
        │         LLMError ─────► escalate("llm_unavailable")
        │         no tool call ─► nudge (counts as invalid)
        │       for each tool call:
        │         unknown / bad args ─► error result to model (counts)
        │         guardrails.check ───► blocked result to model (counts)
        │         terminal tool ──────► finish
        │         read/side-effect ───► carrier client ─► result to model
        └────────────────────────────┘
                      ▼
          AgentOutcome ─► CaseResolved | CaseEscalated   (always exactly one)
```

- **Tools** (`tools.py`): `get_tracking`, `get_rebooking_options` (read);
  `rebook_shipment`, `request_customs_documents`, `open_damage_claim` (side effects);
  `resolve_case`, `escalate_to_human` (terminal). Each tool's arguments are a pydantic model
  that produces the JSON schema sent to the model and validates the reply.
- **Guardrails** (`guardrails.py`), enforced before execution, with the reason returned to
  the model: only the case's own shipment; rebook only an *offered* option, once, within
  the £150 cost-delta cap; claims ≤ declared value and ≤ £2,000, once; customs request once;
  customer messages may not mention refund / compensation / guarantee; a resolution code
  is accepted only if backed by the matching successful action; temperature-controlled
  goods and shipments with declared value ≥ £10,000 (the router's high-value threshold,
  repeated here as a second line of defence) may only be read and escalated (rebook,
  claim, customs and resolve are blocked).
- **Budgets** (`AgentBudget`): 8 model calls, $0.05, 90 s, 3 invalid calls (defaults, all
  configurable). The wall-clock budget is a hard deadline: each model call receives only the
  time left, which bounds its HTTP timeout, its retries and its backoff sleeps. The agent
  worker sets Kafka's `max.poll.interval.ms` (default 600 s) and refuses to start if that is
  not above its worst case per record, so a slow run cannot get the consumer evicted. A breach, an LLM outage, or the model's own `escalate_to_human` all
  produce `CaseEscalated`, carrying any actions already taken.
- **LLM client** (`llm.py`): `OpenRouterChat` over an OpenAI-compatible endpoint, with
  native function calling, retry on 408/409/425/429/5xx (Retry-After honoured), and token
  usage plus cost per call. With no API key the worker uses `UnavailableChat`, so agent
  cases escalate instead of stalling. `ScriptedChat` replays fixed turns for tests/evals.
- **Prompt-injection stance:** carrier free text is declared untrusted in the system
  prompt, and the guardrails (not the prompt) are what stop cross-shipment actions and
  unbacked promises.

## 7. carrier-gateway (`services/carrier-gateway`)

A TypeScript/Fastify service, with zod validation, standing in for a carrier integration.

| Endpoint | Notes |
|---|---|
| `GET /v1/shipments/:id/tracking` | status, location, ETA, history, missing customs documents |
| `GET /v1/shipments/:id/rebooking-options` | option ids, new ETA, cost delta |
| `POST /v1/rebookings` | requires `Idempotency-Key` |
| `POST /v1/customs-document-requests` | requires `Idempotency-Key` |
| `POST /v1/claims` | requires `Idempotency-Key`; used by the agent and the auto-claim playbook |
| `PUT /v1/_admin/shipments/:id` | seed data; only when `ADMIN_ENABLED=true` |

Idempotency: the same key and body replays the original response (`Idempotent-Replayed:
true`); the same key with a different body is rejected with 422. Keys are derived as
`<case_id>:rebook`, `<case_id>:customs_docs` and `<case_id>:claim`, so a redelivered run
cannot act twice. Claims go through the gateway from both the agent and the auto-claim
playbook: playbooks stay pure functions that *decide* on a claim, and the playbook worker
files it, retrying transient failures and escalating (source `playbook`) if the carrier
refuses, so a customer is never told about a claim that does not exist.
`FAULT_RATE` and `LATENCY_MS` inject 503s (with `Retry-After`) and latency.

The Python `HttpCarrierClient` adds a timeout, retry on transient statuses, a circuit
breaker (5 consecutive failures opens it for 30 s), and injects `traceparent`. Responses are
validated against pydantic models that mirror the zod schemas.

## 8. State

Postgres holds two tables, created on startup:

- `ingested_events(event_id, fingerprint, received_at)` — ingest's ledger. Same id and
  same content fingerprint: accepted as a duplicate (202). Same id, different content: a
  carrier correction, rejected with 409 rather than silently dropped.
- `processed_events(consumer, event_id, processed_at)` — the idempotency ledger, primary
  key `(consumer, event_id)`.
- `open_cases(shipment_id, case_id, opened_at)` — at most one open automated case per
  shipment; opened by the router, closed by the notifier.
- `agent_outcomes(case_id, event_type, payload, created_at)` — the first finished agent
  outcome per case, republished on redelivery.
- `notification_outbox(notification_id, case_id, shipment_id, channel, body, created_at)` —
  `notification_id` is `<case_id>:customer` or `<case_id>:ops`, so a repeated insert is a
  no-op (`ON CONFLICT DO NOTHING`).

Every store has an in-memory twin behind the same Protocol.

## 9. Observability (ADR 0004)

- **Traces:** OpenTelemetry, exported over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set
  (a no-op otherwise). `traceparent` is injected into Kafka headers on publish and extracted
  on consume, and sent on carrier-gateway calls. Spans: `ingest shipment event`,
  `consume <topic>`, `route`, `playbook <name>`, `agent.run`, `chat <model>` (with
  `gen_ai.*` token attributes and cost), `execute_tool <name>`.
- **Metrics (Prometheus):** `shipex_events_consumed_total{consumer,outcome}`,
  `shipex_handler_seconds`, `shipex_routes_total{route,rule_id}`,
  `shipex_agent_outcomes_total{status}`, `shipex_llm_tokens_total`,
  `shipex_llm_cost_usd_total`, `shipex_guardrail_blocks_total{tool}`,
  `shipex_dead_letters_total{topic}`. Workers expose them on port 9100; ingest-api on `/metrics`.

## 10. Deployment (`docker-compose.yml`)

Ten containers: Redpanda, Postgres, Jaeger, Prometheus, carrier-gateway, ingest-api and the
four workers. Topics (and their DLQs) are created on service start with 3 partitions and
replication factor 1.

**Scaling model:** records are keyed by `shipment_id` across 3 partitions, so each worker
role can run up to 3 instances in its consumer group. The agent worker is the slow, costly
one and is the first to scale; it has its own queue so it never delays playbook cases.

## 11. Testing seams

| Seam | Production | Test / eval |
|---|---|---|
| Bus | Kafka adapters | `InMemoryBus` (per-group committed offsets, redelivery) |
| LLM | `OpenRouterChat` | `ScriptedChat`, `httpx.MockTransport` |
| Carrier | `HttpCarrierClient` | `InMemoryCarrier` (idempotency + injectable faults) |
| Stores | Postgres | in-memory |
| Whole graph | compose stack | `InProcessPipeline` (same handlers and `ConsumerRunner`) |

The offline evaluation (`evals/`) runs 32 scenarios, each delivered twice, through
`InProcessPipeline`. It re-derives policy violations from the emitted events rather than
trusting the guardrail code, and `evals/failure_suite.py` covers the delivery layer. Both
run in CI.

## 12. Known limitations and trade-offs

- **In-process retries block the partition** for up to `max_attempts × max_delay`
  (seconds); a retry topic would avoid this at the cost of ordering and more topics.
- **Ledger/publish window:** a crash between `flush` and `mark` replays a record. This is
  safe only because ids are deterministic and side effects are keyed; a new side effect
  without an idempotency key would break the guarantee.
- **Crash during an agent run:** the outcome is stored only when the run finishes, so a
  worker killed mid-run re-runs the agent. Repeats of the same action are absorbed by
  idempotency keys; a different action on the second run is still possible in that window.
  Journalling each action as it executes would close it.
- **Single Postgres** serves the ledger, outbox, case registry and agent outcomes;
  per-service stores would be the production shape.
- **Escalated cases close when the ops ticket is written.** Automation then treats the
  shipment as free, so a later exception can open a new automated case while a human is
  still working the escalated one. A "held by ops" state with an API to release it would
  close this gap.
- **carrier-gateway state is in memory** and lost on restart; it is a fixture-backed stand-in.
- **No authentication** on ingest-api or the gateway; the admin endpoint is local-only.
- **DLQ replay is manual.** There is no automated reprocessing tool.
- **Agent quality is lightly measured.** Offline results validate the harness, not the
  model. The live run is one sample of one model on a scenario set revised after a first
  run; see the README's evaluation section.
