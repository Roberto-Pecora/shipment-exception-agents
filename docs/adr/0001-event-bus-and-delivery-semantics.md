# ADR 0001 — Event bus and delivery semantics

**Status:** accepted

## Context
Carrier webhooks arrive in bursts, are re-sent by carriers, and some downstream work
(an LLM agent run, a carrier API call) is slow and occasionally fails. Services must scale
and fail independently, and no exception may be silently lost.

## Decision
- **Kafka API (Redpanda locally)**, one topic per hand-off: `shipment.events.v1` →
  `exceptions.playbook.v1` / `exceptions.agent.v1` → `resolutions.v1` / `escalations.v1`.
  Separate playbook and agent queues let the slow, expensive agent workers scale (and
  back up) without delaying deterministic cases. Records are keyed by `shipment_id`, so
  events for one shipment stay ordered within a partition.
- **At-least-once + idempotent consumers.** Offsets are committed only after outputs are
  published and flushed. Each consumer keeps a processed-event ledger (Postgres
  `processed_events`). Output event ids are `uuid5(input_id, salt)`, so a crash between
  publish and commit re-emits *identical* ids that the next consumer dedupes.
- **Side effects carry idempotency keys** (`<case_id>:rebook`), enforced by the
  carrier-gateway, so a replayed agent run cannot rebook twice.
- **Bounded in-process retries, then DLQ.** Transient errors retry with jittered
  exponential backoff (default 3 attempts); permanent errors and poison records go
  straight to `<topic>.dlq` with the error, attempt count and source offset in headers.
  The offset is then committed so one bad record never blocks its partition.

## Consequences
- Retries block the partition for at most `max_attempts × max_delay` (seconds). A delayed
  retry topic would avoid head-of-line blocking but adds a topic per stage and
  re-ordering; not worth it at this scale. Revisit if agent latency dominates.
- DLQ replay is a manual, audited operation (republish to the source topic); deliberate.
- Exactly-once (Kafka transactions) was rejected: the outbox and carrier side effects live
  outside Kafka, so transactions would not cover them anyway. Idempotency does.
