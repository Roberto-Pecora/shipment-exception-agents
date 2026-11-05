"""Chat-model interface with native function calling.

`OpenRouterChat` talks to any OpenAI-compatible endpoint (OpenRouter by default), with
retry/backoff on transient statuses and per-call usage + cost. `ScriptedChat` replays a
fixed list of turns so the agent loop, guardrails and failure handling are tested
hermetically (no key, no network).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from shipex.resilience import RETRYABLE_STATUS, call_with_retry

Message = dict[str, Any]

# (prompt, completion) USD per 1M tokens — fallback when the provider doesn't report cost.
# Snapshot shared with financial-rag-aws; unknown models cost 0.0.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "meta-llama/llama-3.3-70b-instruct": (0.12, 0.30),
    "qwen/qwen-2.5-72b-instruct": (0.12, 0.39),
    "openai/gpt-4o-mini": (0.15, 0.60),
}


class LLMError(Exception):
    """The model could not produce a reply (after retries)."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as emitted by the model


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class ChatReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    usage: Usage = field(default_factory=Usage)

    def as_message(self) -> Message:
        msg: Message = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.tool_calls
            ]
        return msg


class ChatModel(Protocol):
    model: str

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[Message],
        timeout_s: float | None = None,
    ) -> ChatReply:
        """`timeout_s` is a hard deadline for the whole call, retries included."""
        ...


def cost_from_usage(model: str, usage: dict[str, Any]) -> float:
    if isinstance(usage.get("cost"), int | float):  # OpenRouter usage accounting
        return float(usage["cost"])
    p_in, p_out = DEFAULT_PRICES.get(model, (0.0, 0.0))
    prompt = int(usage.get("prompt_tokens", 0))
    completion = int(usage.get("completion_tokens", 0))
    return (prompt * p_in + completion * p_out) / 1_000_000.0


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, httpx.TransportError)


def _retry_after(exc: Exception) -> float | None:
    if isinstance(exc, httpx.HTTPStatusError):
        header = exc.response.headers.get("Retry-After")
        try:
            return float(header) if header else None
        except ValueError:
            return None
    return None


class OpenRouterChat:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_s: float = 60.0,
        max_attempts: int = 3,
        client: httpx.Client | None = None,
        retry_base_delay: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the live agent")
        self.model = model
        self._timeout_s = timeout_s
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._clock = clock
        self._sleep = sleep
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[Message],
        timeout_s: float | None = None,
    ) -> ChatReply:
        # One deadline for the whole call: each attempt's HTTP timeout, the backoff sleeps
        # and the decision to retry are all bounded by the time that is left.
        deadline = self._clock() + (self._timeout_s if timeout_s is None else timeout_s)

        def remaining() -> float:
            return deadline - self._clock()

        if remaining() <= 0:
            raise LLMError("deadline exceeded before the call started")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "tools": list(tools),
            "tool_choice": "auto",
            "temperature": 0,
            "usage": {"include": True},
        }

        def post() -> dict[str, Any]:
            budget = min(self._timeout_s, remaining())
            if budget <= 0:
                raise httpx.TimeoutException("deadline exceeded")
            resp = self._client.post(
                "/chat/completions", json=payload, headers=self._headers, timeout=budget
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data

        try:
            data = call_with_retry(
                post,
                is_retryable=lambda exc: _is_retryable(exc) and remaining() > 1.0,
                retry_after=_retry_after,
                max_attempts=self._max_attempts,
                base_delay=self._retry_base_delay,
                sleep=lambda delay: self._sleep(max(0.0, min(delay, remaining() - 1.0))),
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        return self._parse(data)

    def _parse(self, data: dict[str, Any]) -> ChatReply:
        if "error" in data:
            raise LLMError(f"provider error: {data['error']}")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed response: {exc}") from exc
        calls = tuple(
            ToolCall(
                id=str(c.get("id") or f"call_{i}"),
                name=str(c.get("function", {}).get("name", "")),
                arguments=str(c.get("function", {}).get("arguments") or "{}"),
            )
            for i, c in enumerate(message.get("tool_calls") or [])
        )
        usage_raw: dict[str, Any] = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0)),
            completion_tokens=int(usage_raw.get("completion_tokens", 0)),
            cost_usd=cost_from_usage(self.model, usage_raw),
        )
        return ChatReply(content=message.get("content"), tool_calls=calls, usage=usage)


class UnavailableChat:
    """Stand-in when no provider is configured: every call fails, so every agent case
    escalates with `llm_unavailable` — the system degrades to humans, it doesn't stall."""

    def __init__(self, reason: str, model: str = "unconfigured") -> None:
        self.model = model
        self._reason = reason

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[Message],
        timeout_s: float | None = None,
    ) -> ChatReply:
        raise LLMError(self._reason)


@dataclass(frozen=True)
class ScriptedTurn:
    """One canned model reply: tool calls, plain text, or a simulated provider failure."""

    tool_calls: tuple[tuple[str, dict[str, Any] | str], ...] = ()
    content: str | None = None
    error: str | None = None


SCRIPTED_USAGE = Usage(prompt_tokens=400, completion_tokens=60)


class ScriptedChat:
    """Replays turns in order; raises LLMError when the script runs out."""

    def __init__(
        self,
        turns: Sequence[ScriptedTurn],
        model: str = "scripted",
        usage: Usage | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.model = model
        self._turns = list(turns)
        self._usage = usage or SCRIPTED_USAGE
        self._latency_s = latency_s
        self.seen: list[list[Message]] = []
        self.timeouts: list[float | None] = []

    def chat(
        self,
        messages: Sequence[Message],
        tools: Sequence[Message],
        timeout_s: float | None = None,
    ) -> ChatReply:
        self.seen.append(list(messages))
        self.timeouts.append(timeout_s)
        if self._latency_s:
            time.sleep(self._latency_s)
        if not self._turns:
            raise LLMError("script exhausted")
        turn = self._turns.pop(0)
        if turn.error:
            raise LLMError(turn.error)
        calls = tuple(
            ToolCall(
                id=f"call_{len(self.seen)}_{i}",
                name=name,
                arguments=args if isinstance(args, str) else json.dumps(args),
            )
            for i, (name, args) in enumerate(turn.tool_calls)
        )
        return ChatReply(content=turn.content, tool_calls=calls, usage=self._usage)

    @classmethod
    def from_spec(cls, spec: Sequence[dict[str, Any]]) -> ScriptedChat:
        """Build from JSON-friendly dicts (used by eval scenarios)."""
        turns = [
            ScriptedTurn(
                tool_calls=tuple(
                    (c["name"], c.get("arguments", {})) for c in t.get("tool_calls", [])
                ),
                content=t.get("content"),
                error=t.get("error"),
            )
            for t in spec
        ]
        return cls(turns)
