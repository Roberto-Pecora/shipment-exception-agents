"""The offline eval is a CI gate, so it must pass today *and* fail when guardrails break."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipex.agent import loop
from shipex.agent.llm import ChatModel, ScriptedChat
from shipex.evaluation import Scenario, load_scenarios, run_scenario, summarise

SCENARIOS = load_scenarios(Path(__file__).parents[1] / "evals" / "scenarios.jsonl")


def scripted(s: Scenario) -> ChatModel:
    return ScriptedChat.from_spec(json.loads(json.dumps(s.script).replace("$SID", f"SHP-{s.id}")))


def test_offline_suite_meets_the_gate() -> None:
    summary = summarise([run_scenario(s, scripted) for s in SCENARIOS])
    assert summary.route_accuracy == 1.0
    assert summary.outcome_accuracy == 1.0
    assert summary.policy_violations == 0
    assert summary.duplicate_notifications == 0
    assert summary.handled_without_llm == 10  # 3 human + 7 playbook never touch the model


def test_gate_detects_disabled_guardrails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Violations are re-derived from emitted events, not trusted from the guardrail code."""
    monkeypatch.setattr(loop, "check", lambda *args: None)
    results = {s.id: run_scenario(s, scripted) for s in SCENARIOS}
    assert summarise(list(results.values())).policy_violations > 0
    assert any("over cap" in v for v in results["rebook-only-over-cap"].violations)
    assert any("over cap" in v for v in results["damage-above-agent-cap"].violations)
    assert any("without a rebook" in v for v in results["premature-resolution"].violations)
