"""Kafka (Redpanda) adapters for the bus Protocols.

Producer: idempotent, acks=all; `flush()` raises if any delivery failed, so the runner
never commits an input whose outputs were lost. Consumer: auto-commit off; the runner
commits each offset explicitly after its outputs are durable.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from confluent_kafka import Consumer, KafkaError, Producer, TopicPartition
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import NewTopic

from shipex.bus.base import Record
from shipex.bus.consumer import TransientError
from shipex.contracts.events import dlq_topic

logger = logging.getLogger(__name__)


class KafkaPublisher:
    def __init__(self, bootstrap: str) -> None:
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "enable.idempotence": True,
                "acks": "all",
                "linger.ms": 5,
            }
        )
        self._errors: list[str] = []

    def _on_delivery(self, err: Any, msg: Any) -> None:
        if err is not None:
            self._errors.append(f"{msg.topic()}: {err}")

    def publish(
        self, topic: str, key: str, value: bytes, headers: Mapping[str, str] | None = None
    ) -> None:
        self._producer.produce(
            topic,
            key=key.encode("utf-8"),
            value=value,
            headers=[(k, v.encode("utf-8")) for k, v in (headers or {}).items()],
            on_delivery=self._on_delivery,
        )
        self._producer.poll(0)

    def flush(self, timeout: float = 10.0) -> None:
        remaining = self._producer.flush(timeout)
        errors, self._errors = self._errors, []
        if remaining or errors:
            raise TransientError(f"undelivered={remaining} errors={errors[:3]}")


class KafkaSubscriber:
    def __init__(
        self,
        bootstrap: str,
        group: str,
        topics: Iterable[str],
        max_poll_interval_ms: int = 300_000,
    ) -> None:
        # If one record takes longer than max.poll.interval.ms, the broker evicts this
        # consumer and redelivers its partitions; the worker sets it above its worst case.
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": group,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                "max.poll.interval.ms": max_poll_interval_ms,
            }
        )
        self._consumer.subscribe(list(topics))

    def poll(self, timeout: float) -> Record | None:
        msg = self._consumer.poll(timeout)
        if msg is None:
            return None
        err = msg.error()
        if err is not None:
            if err.code() != KafkaError._PARTITION_EOF:
                logger.warning("consumer error: %s", err)
            return None
        topic, value = msg.topic(), msg.value()
        partition, offset = msg.partition(), msg.offset()
        if topic is None or value is None or partition is None or offset is None:
            logger.warning("skipping record without topic/value/position")
            return None
        return Record(topic, _text(msg.key()), value, _headers(msg.headers()), partition, offset)

    def commit(self, record: Record) -> None:
        self._consumer.commit(
            offsets=[TopicPartition(record.topic, record.partition, record.offset + 1)],
            asynchronous=False,
        )

    def close(self) -> None:
        self._consumer.close()


def _text(raw: str | bytes | None) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _headers(raw: Any) -> dict[str, str]:
    """Normalise librdkafka headers (list of pairs or dict, str or bytes values)."""
    pairs = raw.items() if isinstance(raw, dict) else (raw or [])
    return {str(k): _text(v) for k, v in pairs if v is not None}


def ensure_topics(bootstrap: str, topics: Iterable[str], partitions: int = 3) -> None:
    """Create each topic and its DLQ if missing (idempotent)."""
    admin = AdminClient({"bootstrap.servers": bootstrap})
    names = sorted({n for t in topics for n in (t, dlq_topic(t))})
    futures = admin.create_topics(
        [NewTopic(n, num_partitions=partitions, replication_factor=1) for n in names]
    )
    for name, future in futures.items():
        try:
            future.result()
            logger.info("created topic %s", name)
        except Exception as exc:
            if "TOPIC_ALREADY_EXISTS" not in str(exc):
                raise
