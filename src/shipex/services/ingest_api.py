"""ingest-api: the carrier webhook. Validates, dedupes, publishes — nothing else.

Run: uvicorn shipex.services.ingest_api:app_from_env --factory --port 8080
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from opentelemetry import trace
from prometheus_client import make_asgi_app
from pydantic import BaseModel

from shipex import telemetry
from shipex.bus.base import Publisher
from shipex.contracts.events import Envelope, ShipmentEvent, Topic
from shipex.store.ingest import IngestLedger, fingerprint


class IngestAccepted(BaseModel):
    event_id: str
    duplicate: bool


def create_app(publisher: Publisher, ledger: IngestLedger) -> FastAPI:
    app = FastAPI(title="shipex ingest-api", version="0.1.0")

    @app.post(
        "/v1/shipment-events",
        status_code=202,
        response_model=IngestAccepted,
        responses={409: {"description": "carrier_event_id reused with different content"}},
    )
    def ingest(event: ShipmentEvent) -> IngestAccepted:
        """Accept a carrier status event.

        Re-sending the same carrier_event_id with the same content is a no-op (202,
        duplicate). The same id with *different* content is a correction we refuse to drop
        silently: 409, so the carrier re-sends it under a new id or an operator looks.
        """
        env = Envelope.for_shipment_event(event)
        digest = fingerprint(event)
        with telemetry.tracer.start_as_current_span(
            "ingest shipment event",
            kind=trace.SpanKind.PRODUCER,
            attributes={"shipex.event_id": env.event_id, "shipex.shipment_id": env.key},
        ) as span:
            stored = ledger.lookup(env.event_id)
            if stored == digest:
                return IngestAccepted(event_id=env.event_id, duplicate=True)
            if stored is not None:
                span.set_attribute("shipex.ingest.conflict", True)
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"carrier_event_id {event.carrier_event_id!r} was already received with "
                        "different content; send corrections under a new carrier_event_id"
                    ),
                )
            publisher.publish(
                Topic.SHIPMENT_EVENTS,
                env.key,
                env.to_bytes(),
                telemetry.inject_headers({"event_type": env.event_type}),
            )
            publisher.flush()
            ledger.record(env.event_id, digest)
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
    from shipex.store.ingest import PostgresIngestLedger

    settings = Settings()
    telemetry.configure("shipex-ingest-api")
    ensure_topics(settings.kafka_bootstrap, list(Topic), settings.topic_partitions)
    conn = psycopg.connect(settings.database_url)
    return create_app(KafkaPublisher(settings.kafka_bootstrap), PostgresIngestLedger(conn))
