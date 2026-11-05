"""One trace must span services: traceparent travels in bus headers."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shipex.agent.carrier import InMemoryCarrier
from shipex.agent.llm import ScriptedChat, ScriptedTurn
from shipex.agent.loop import ShipmentExceptionAgent
from shipex.pipeline import InProcessPipeline
from shipex.services.ingest_api import create_app
from tests.conftest import make_event

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def sdk_provider() -> Iterator[None]:
    # The global provider can only be set once per process; this module owns it.
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace.set_tracer_provider(provider)
    yield


def test_trace_spans_ingest_router_agent_and_notifier(carrier: InMemoryCarrier) -> None:
    from fastapi.testclient import TestClient

    EXPORTER.clear()
    script = [
        ScriptedTurn(tool_calls=(("get_tracking", {"shipment_id": "SHP-1"}),)),
        ScriptedTurn(tool_calls=(("escalate_to_human", {"reason": "needs a specialist"}),)),
    ]
    p = InProcessPipeline(ShipmentExceptionAgent(ScriptedChat(script), carrier))
    api = TestClient(create_app(p.bus, p.idempotency))
    assert (
        api.post(
            "/v1/shipment-events", json=make_event(delay_hours=72.0).model_dump(mode="json")
        ).status_code
        == 202
    )
    p.drain()

    spans = EXPORTER.get_finished_spans()
    names = {s.name for s in spans}
    assert {
        "ingest shipment event",
        "route",
        "agent.run",
        "chat scripted",
        "execute_tool get_tracking",
    } <= names
    assert "consume escalations.v1" in names
    assert len({s.context.trace_id for s in spans}) == 1  # one trace, end to end

    chat = next(s for s in spans if s.name == "chat scripted")
    assert chat.attributes is not None
    assert chat.attributes["gen_ai.usage.input_tokens"] == 400
