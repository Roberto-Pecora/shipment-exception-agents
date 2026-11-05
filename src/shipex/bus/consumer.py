"""The one consume loop every worker uses.

Delivery contract (see docs/adr/0001):
- **At-least-once**: offsets are committed only after outputs are published and flushed.
- **Idempotent**: a per-consumer ledger skips event ids already processed; output ids are
  derived from input ids, so a crash between publish and commit re-emits identical ids
  that downstream consumers dedupe.
- **Bounded retries**: `TransientError` (and unexpected exceptions) retry in-process with
  jittered exponential backoff; `PermanentError` or a poison record goes straight to the
  DLQ. Exhausted retries also go to the DLQ, with the error in headers, and the offset is
  committed so one bad record never blocks the partition forever.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from opentelemetry import trace
from pydantic import ValidationError

from shipex import telemetry
from shipex.bus.base import Publisher, Record, Subscriber
from shipex.contracts.events import Envelope, dlq_topic
from shipex.store.idempotency import IdempotencyStore

logger = logging.getLogger(__name__)

Outcome = Literal["processed", "duplicate", "dead_lettered"]


class TransientError(Exception):
    """A dependency is temporarily unavailable; the same input may succeed later."""


class PermanentError(Exception):
    """The input can never be processed (contract violation, bad payload)."""


@dataclass(frozen=True)
class Outgoing:
    topic: str
    envelope: Envelope


Handler = Callable[[Envelope], Sequence[Outgoing]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_s: float = 0.2
    max_delay_s: float = 5.0

    def delay(self, attempt: int) -> float:
        capped = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
        return float(capped * (0.5 + random.random() / 2))


class ConsumerRunner:
    def __init__(
        self,
        name: str,
        subscriber: Subscriber,
        publisher: Publisher,
        handler: Handler,
        idempotency: IdempotencyStore,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.name = name
        self._sub = subscriber
        self._pub = publisher
        self._handler = handler
        self._idem = idempotency
        self._retry = retry or RetryPolicy()
        self._sleep = sleep

    def run_once(self, timeout: float = 1.0) -> Outcome | None:
        record = self._sub.poll(timeout)
        if record is None:
            return None
        outcome = self.process(record)
        telemetry.EVENTS_CONSUMED.labels(self.name, outcome).inc()
        return outcome

    def run_forever(self, stop: threading.Event) -> None:
        logger.info("%s: consuming", self.name)
        try:
            while not stop.is_set():
                self.run_once()
        finally:
            self._pub.flush()
            self._sub.close()
            logger.info("%s: stopped", self.name)

    def process(self, record: Record) -> Outcome:
        try:
            envelope = Envelope.from_bytes(record.value)
        except (ValidationError, ValueError) as exc:
            return self._dead_letter(record, f"poison record: {exc}", attempts=0)

        if self._idem.seen(self.name, envelope.event_id):
            self._sub.commit(record)
            return "duplicate"

        ctx = telemetry.extract_context(record.headers)
        with telemetry.tracer.start_as_current_span(
            f"consume {record.topic}",
            context=ctx,
            kind=trace.SpanKind.CONSUMER,
            attributes={
                "messaging.system": "kafka",
                "messaging.destination.name": record.topic,
                "messaging.consumer.group.name": self.name,
                "shipex.event_id": envelope.event_id,
                "shipex.correlation_id": envelope.correlation_id,
            },
        ):
            outputs, error, attempts = self._handle_with_retry(envelope)
            if error is not None:
                return self._dead_letter(record, error, attempts)
            for out in outputs:
                self._pub.publish(
                    out.topic,
                    out.envelope.key,
                    out.envelope.to_bytes(),
                    telemetry.inject_headers({"event_type": out.envelope.event_type}),
                )
            self._pub.flush()

        self._idem.mark(self.name, envelope.event_id)
        self._sub.commit(record)
        return "processed"

    def _handle_with_retry(self, envelope: Envelope) -> tuple[Sequence[Outgoing], str | None, int]:
        for attempt in range(1, self._retry.max_attempts + 1):
            started = time.perf_counter()
            try:
                return self._handler(envelope), None, attempt
            except (PermanentError, ValidationError) as exc:
                return (), f"permanent: {exc}", attempt
            except Exception as exc:  # TransientError or unexpected: retry, then DLQ
                logger.warning(
                    "%s: attempt %d/%d failed for %s: %s",
                    self.name,
                    attempt,
                    self._retry.max_attempts,
                    envelope.event_id,
                    exc,
                )
                if attempt == self._retry.max_attempts:
                    return (), f"exhausted retries: {type(exc).__name__}: {exc}", attempt
                self._sleep(self._retry.delay(attempt))
            finally:
                telemetry.HANDLER_SECONDS.labels(self.name).observe(time.perf_counter() - started)
        raise AssertionError("unreachable")

    def _dead_letter(self, record: Record, error: str, attempts: int) -> Outcome:
        topic = dlq_topic(record.topic)
        headers = {
            **record.headers,
            "x-error": error[:1000],
            "x-attempts": str(attempts),
            "x-consumer": self.name,
            "x-source-offset": str(record.offset),
        }
        self._pub.publish(topic, record.key, record.value, headers)
        self._pub.flush()
        self._sub.commit(record)
        telemetry.DEAD_LETTERS.labels(topic).inc()
        logger.error(
            "%s: dead-lettered offset %d to %s: %s", self.name, record.offset, topic, error
        )
        return "dead_lettered"
