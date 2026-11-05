"""Run the scenario evaluation.

  python evals/run_eval.py                         # offline (scripted model): CI gate
  python evals/run_eval.py --mode live --strategy both   # real model via OpenRouter

Offline mode replays each scenario's scripted model turns, so it measures the harness —
routing, guardrails, policy violations, failure handling, duplicate suppression — not
model quality. Live mode needs OPENROUTER_API_KEY and spends money.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from shipex.agent.llm import ChatModel, OpenRouterChat, ScriptedChat
from shipex.agent.loop import AgentBudget
from shipex.evaluation import (
    Scenario,
    ScenarioResult,
    load_scenarios,
    render_markdown,
    results_json,
    run_scenario,
    summarise,
)

HERE = Path(__file__).parent


def scripted(s: Scenario) -> ChatModel:
    spec = json.loads(json.dumps(s.script).replace("$SID", f"SHP-{s.id}"))
    return ScriptedChat.from_spec(spec)


def live_factory(model: str) -> Callable[[Scenario], ChatModel]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("live mode needs OPENROUTER_API_KEY")
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    return lambda s: OpenRouterChat(model, key, base)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument("--strategy", choices=["hybrid", "agent_only", "both"], default="hybrid")
    parser.add_argument("--model", default=os.environ.get("AGENT_MODEL", "qwen/qwen3-30b-a3b"))
    parser.add_argument("--scenarios", type=Path, default=HERE / "scenarios.jsonl")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--gate", action="store_true", help="exit 1 if offline CI gate fails")
    parser.add_argument("--max-cost-usd", type=float, default=None, help="per-case agent budget")
    parser.add_argument("--max-wall-s", type=float, default=None, help="per-case agent budget")
    args = parser.parse_args()

    defaults = AgentBudget()
    budget = AgentBudget(
        max_cost_usd=args.max_cost_usd or defaults.max_cost_usd,
        max_wall_s=args.max_wall_s or defaults.max_wall_s,
    )

    scenarios = load_scenarios(args.scenarios)
    chat_for: Callable[[Scenario], ChatModel] = scripted
    if args.mode == "live":
        scenarios = [s for s in scenarios if not s.offline_only]
        chat_for = live_factory(args.model)

    strategies = ["hybrid", "agent_only"] if args.strategy == "both" else [args.strategy]
    runs: dict[str, list[ScenarioResult]] = {}
    for strategy in strategies:
        runs[strategy] = [
            run_scenario(s, chat_for, agent_only=strategy == "agent_only", budget=budget)
            for s in scenarios
        ]

    if args.mode == "offline":
        title = "Offline evaluation (scripted model)"
        note = (
            "Scripted model turns: these numbers measure routing, guardrails, policy "
            "enforcement, failure handling and duplicate suppression — not model quality. "
            "Latency here is in-process and not meaningful."
        )
    else:
        title = f"Live evaluation — {args.model}"
        note = "Real model via OpenRouter. Cost is as reported by OpenRouter usage accounting."
    out = args.out or HERE / "results" / (
        "offline.md" if args.mode == "offline" else f"live-{args.model.replace('/', '_')}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(title, runs, note), encoding="utf-8")
    out.with_suffix(".json").write_text(results_json(runs), encoding="utf-8")

    for name, results in runs.items():
        s = summarise(results)
        print(
            f"[{name}] scenarios={s.scenarios} route_acc={s.route_accuracy:.3f} "
            f"outcome_acc={s.outcome_accuracy:.3f} violations={s.policy_violations} "
            f"dupes={s.duplicate_notifications} without_llm={s.handled_without_llm}/{s.exceptions} "
            f"llm_calls={s.llm_calls} blocks={s.guardrail_blocks} cost=${s.cost_usd:.4f}"
        )
    print(f"wrote {out}")

    if args.gate:
        s = summarise(runs["hybrid"])
        failures = [
            msg
            for ok, msg in [
                (s.route_accuracy == 1.0, f"route accuracy {s.route_accuracy:.3f} < 1.0"),
                (s.outcome_accuracy == 1.0, f"outcome accuracy {s.outcome_accuracy:.3f} < 1.0"),
                (s.policy_violations == 0, f"{s.policy_violations} policy violations"),
                (s.duplicate_notifications == 0, f"{s.duplicate_notifications} duplicates"),
            ]
            if not ok
        ]
        for msg in failures:
            print(f"GATE FAILED: {msg}")
        return 1 if failures else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
