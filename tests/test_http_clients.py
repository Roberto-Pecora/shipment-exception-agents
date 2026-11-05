"""OpenRouter and carrier-gateway clients against httpx.MockTransport (no network)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from shipex.agent.carrier import CarrierError, HttpCarrierClient
from shipex.agent.llm import LLMError, OpenRouterChat
from shipex.resilience import CircuitBreaker

Responder = Callable[[httpx.Request], httpx.Response]


def client(handler: Responder, base_url: str) -> httpx.Client:
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))


def openrouter(handler: Responder) -> OpenRouterChat:
    return OpenRouterChat(
        "qwen/qwen3-30b-a3b",
        "test-key",
        client=client(handler, "https://openrouter.test/api/v1"),
        retry_base_delay=0.0,
    )


COMPLETION = {
    "choices": [
        {
            "message": {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_tracking", "arguments": '{"shipment_id":"S"}'},
                    }
                ],
            }
        }
    ],
    "usage": {"prompt_tokens": 900, "completion_tokens": 40, "cost": 0.00042},
}


def test_openrouter_request_and_parse() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=COMPLETION)

    reply = openrouter(handler).chat([{"role": "user", "content": "hi"}], [{"type": "function"}])
    assert reply.tool_calls[0].name == "get_tracking"
    assert reply.usage.cost_usd == pytest.approx(0.00042)
    assert seen[0]["tool_choice"] == "auto" and seen[0]["usage"] == {"include": True}
    assert reply.as_message()["tool_calls"][0]["function"]["name"] == "get_tracking"


def test_openrouter_retries_429_then_succeeds() -> None:
    statuses = iter([429, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json=COMPLETION if status == 200 else {"error": "busy"})

    assert openrouter(handler).chat([], []).tool_calls


def test_openrouter_auth_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(LLMError):
        openrouter(handler).chat([], [])
    assert calls["n"] == 1


def test_openrouter_malformed_body_is_an_llm_error() -> None:
    with pytest.raises(LLMError, match="malformed"):
        openrouter(lambda r: httpx.Response(200, json={"choices": []})).chat([], [])


def gateway(handler: Responder, breaker: CircuitBreaker | None = None) -> HttpCarrierClient:
    return HttpCarrierClient(
        "http://gateway.test",
        client=client(handler, "http://gateway.test"),
        breaker=breaker,
        retry_base_delay=0.0,
    )


def test_rebook_sends_idempotency_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Idempotency-Key"] == "case-1:rebook"
        assert json.loads(request.content) == {"shipment_id": "S", "option_id": "o"}
        return httpx.Response(
            201,
            json={
                "rebooking_id": "rb-1",
                "shipment_id": "S",
                "option_id": "o",
                "new_eta": "2026-11-03T10:00:00Z",
            },
        )

    assert gateway(handler).rebook("S", "o", "case-1:rebook").rebooking_id == "rb-1"


def test_gateway_4xx_is_permanent_and_5xx_transient() -> None:
    with pytest.raises(CarrierError) as not_found:
        gateway(lambda r: httpx.Response(404, json={"error": "unknown"})).get_tracking("S")
    assert not not_found.value.transient and not_found.value.status == 404

    with pytest.raises(CarrierError) as unavailable:
        gateway(lambda r: httpx.Response(503)).get_tracking("S")
    assert unavailable.value.transient


def test_circuit_opens_after_repeated_failures() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    breaker = CircuitBreaker("carrier-gateway", failure_threshold=2, reset_timeout_s=60)
    gw = gateway(handler, breaker)
    for _ in range(2):
        with pytest.raises(CarrierError):
            gw.get_tracking("S")
    before = calls["n"]
    with pytest.raises(CarrierError, match="circuit is open"):
        gw.get_tracking("S")
    assert calls["n"] == before  # short-circuited: the gateway was not called
