"""Fix for review finding 1: the agent's wall-clock budget is a hard deadline."""

from __future__ import annotations

import httpx
import pytest

from shipex.agent.carrier import InMemoryCarrier
from shipex.agent.llm import LLMError, OpenRouterChat, ScriptedChat, ScriptedTurn
from shipex.agent.loop import AgentBudget, ShipmentExceptionAgent
from shipex.config import Settings
from tests.conftest import make_case


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_deadline_bounds_retries_and_sleeps() -> None:
    """A provider that keeps timing out cannot hold the call past its deadline."""
    clock = FakeClock()
    attempts: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(clock.now)
        clock.now += 20.0  # each attempt burns 20s before timing out
        raise httpx.ReadTimeout("provider stalled", request=request)

    def sleep(seconds: float) -> None:
        clock.now += seconds

    chat = OpenRouterChat(
        "m",
        "key",
        client=httpx.Client(base_url="https://x.test", transport=httpx.MockTransport(handler)),
        max_attempts=10,
        retry_base_delay=1.0,
        clock=clock,
        sleep=sleep,
    )
    with pytest.raises(LLMError):
        chat.chat([], [], timeout_s=45.0)
    assert clock.now <= 45.0 + 20.0  # at most one in-flight attempt past the last check
    assert len(attempts) < 10  # retries stopped because time ran out, not attempts


def test_no_call_when_the_deadline_has_passed() -> None:
    calls: list[httpx.Request] = []
    chat = OpenRouterChat(
        "m",
        "key",
        client=httpx.Client(
            base_url="https://x.test",
            transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(500)),
        ),
    )
    with pytest.raises(LLMError, match="deadline"):
        chat.chat([], [], timeout_s=0.0)
    assert calls == []


def test_agent_passes_only_the_remaining_budget(carrier: InMemoryCarrier) -> None:
    ticks = iter([0.0, 0.0, 0.0, 30.0, 30.0, 30.0, 30.0, 30.0])
    llm = ScriptedChat(
        [
            ScriptedTurn(tool_calls=(("get_tracking", {"shipment_id": "SHP-1"}),)),
            ScriptedTurn(tool_calls=(("escalate_to_human", {"reason": "needs a person"}),)),
        ]
    )
    agent = ShipmentExceptionAgent(
        llm, carrier, AgentBudget(max_wall_s=90.0), clock=lambda: next(ticks)
    )
    agent.run(make_case())
    assert llm.timeouts == [90.0, 60.0]


def test_worker_refuses_a_poll_interval_below_the_worst_case() -> None:
    Settings(kafka_max_poll_interval_ms=600_000).check_poll_interval()  # defaults are safe
    with pytest.raises(ValueError, match="worst case"):
        Settings(kafka_max_poll_interval_ms=300_000, agent_max_wall_s=120).check_poll_interval()
