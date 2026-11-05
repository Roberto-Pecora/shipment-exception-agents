"""Entry point for the four consumer services (one image, role chosen by argument).

Run: python -m shipex.services.worker {router|playbook|agent|notifier}
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from collections.abc import Sequence

import psycopg

from shipex import telemetry
from shipex.agent.carrier import HttpCarrierClient
from shipex.agent.llm import ChatModel, OpenRouterChat, UnavailableChat
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.bus.consumer import ConsumerRunner, Handler, RetryPolicy
from shipex.bus.kafka import KafkaPublisher, KafkaSubscriber, ensure_topics
from shipex.config import Settings
from shipex.contracts.events import Topic
from shipex.handlers import (
    SUBSCRIPTIONS,
    agent_handler,
    notifier_handler,
    playbook_handler,
    router_handler,
)
from shipex.store.idempotency import PostgresIdempotencyStore
from shipex.store.outbox import PostgresOutbox
from shipex.store.outcomes import PostgresOutcomeStore

logger = logging.getLogger(__name__)


def build_chat_model(settings: Settings) -> ChatModel:
    if not settings.openrouter_api_key:
        logger.warning("OPENROUTER_API_KEY not set: agent cases will escalate to humans")
        return UnavailableChat("no OPENROUTER_API_KEY configured", model=settings.agent_model)
    return OpenRouterChat(
        settings.agent_model,
        settings.openrouter_api_key,
        settings.openrouter_base_url,
        settings.openrouter_timeout_s,
    )


def build_handler(
    role: str, settings: Settings, conn: psycopg.Connection[tuple[object, ...]]
) -> Handler:
    if role == "router":
        return router_handler(settings.routing_policy())
    if role == "playbook":
        return playbook_handler()
    if role == "agent":
        agent = ShipmentExceptionAgent(
            build_chat_model(settings),
            HttpCarrierClient(settings.carrier_gateway_url),
            settings.agent_budget(),
            settings.guardrail_policy(),
        )
        return agent_handler(agent, PostgresOutcomeStore(conn))
    if role == "notifier":
        return notifier_handler(PostgresOutbox(conn))
    raise SystemExit(f"unknown role {role!r}; expected one of {sorted(SUBSCRIPTIONS)}")


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in SUBSCRIPTIONS:
        raise SystemExit(f"usage: python -m shipex.services.worker {{{','.join(SUBSCRIPTIONS)}}}")
    role = args[0]
    settings = Settings()
    if role == "agent":
        settings.check_poll_interval()
    telemetry.configure(f"shipex-{role}")
    ensure_topics(settings.kafka_bootstrap, list(Topic), settings.topic_partitions)

    conn = psycopg.connect(settings.database_url)
    runner = ConsumerRunner(
        role,
        KafkaSubscriber(
            settings.kafka_bootstrap,
            f"shipex-{role}",
            SUBSCRIPTIONS[role],
            max_poll_interval_ms=settings.kafka_max_poll_interval_ms,
        ),
        KafkaPublisher(settings.kafka_bootstrap),
        build_handler(role, settings, conn),
        PostgresIdempotencyStore(conn),
        RetryPolicy(max_attempts=settings.consumer_max_attempts),
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    telemetry.start_metrics_server(settings.metrics_port)
    try:
        runner.run_forever(stop)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
