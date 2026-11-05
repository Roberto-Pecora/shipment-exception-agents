"""ingest-api: the carrier webhook. Validates, dedupes, publishes — nothing else.

Run: uvicorn shipex.services.ingest_api:app_from_env --factory --port 8080
"""

from __future__ import annotations

from fastapi import FastAPI
from opentelemetry import trace
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from shipex import telemetry
from shipex.bus.base import Publisher
from shipex.contracts.events import Envelope, ShipmentEvent, Topic
from shipex.store.idempotency import IdempotencyStore

CONSUMER = "ingest-api"


class IngestAccepted(BaseModel):
    event_id: str
    duplicate: bool


def create_app(publisher: Publisher, idempotency: IdempotencyStore) -> FastAPI:
    app = FastAPI(title="shipex ingest-api", version="0.1.0")

    @app.post("/v1/shipment-events", status_code=202, response_model=IngestAccepted)
    def ingest(event: ShipmentEvent) -> IngestAccepted:
        """Accept a carrier status event. Re-sending the same carrier_event_id is a no-op."""
        env = Envelope.for_shipment_event(event)
        with telemetry.tracer.start_as_current_span(
            "ingest shipment event",
            kind=trace.SpanKind.PRODUCER,
            attributes={"shipex.event_id": env.event_id, "shipex.shipment_id": env.key},
        ):
            if idempotency.seen(CONSUMER, env.event_id):
                return IngestAccepted(event_id=env.event_id, duplicate=True)
            publisher.publish(
                Topic.SHIPMENT_EVENTS,
                env.key,
                env.to_bytes(),
                telemetry.inject_headers({"event_type": env.event_type}),
            )
            publisher.flush()
            idempotency.mark(CONSUMER, env.event_id)
        return IngestAccepted(event_id=env.event_id, duplicate=False)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/metrics", make_asgi_app())
    return app


def app_from_env() -> FastAPI:
    import psycopg

    from shipex.bus.kafka import KafkaPublisher, ensure_topics
    from shipex.config import Settings
    from shipex.store.idempotency import PostgresIdempotencyStore

    settings = Settings()
    telemetry.configure("shipex-ingest-api")
    ensure_topics(settings.kafka_bootstrap, list(Topic), settings.topic_partitions)
    conn = psycopg.connect(settings.database_url)
    return create_app(KafkaPublisher(settings.kafka_bootstrap), PostgresIdempotencyStore(conn))
