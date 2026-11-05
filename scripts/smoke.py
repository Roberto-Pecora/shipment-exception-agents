"""End-to-end smoke test against the docker-compose stack (`make up` first).

Seeds the carrier-gateway with each scenario's shipment, posts every scenario event to
ingest-api twice (duplicates must be absorbed), then waits until the notifier's outbox
holds exactly one row per exception. Without OPENROUTER_API_KEY the agent worker uses
no model, so agent-routed cases escalate to ops: that is the intended degradation.

  python scripts/smoke.py [--ingest http://localhost:8080] [--timeout 120]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

import httpx
import psycopg

from shipex.evaluation import build_carrier, build_event, load_scenarios
from shipex.routing.rules import route

ROOT = Path(__file__).parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ingest", default="http://localhost:8080")
    parser.add_argument("--gateway", default="http://localhost:8081")
    parser.add_argument("--database", default="postgresql://shipex:shipex@localhost:5432/shipex")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    run = time.strftime("%H%M%S")
    scenarios = [s for s in load_scenarios(ROOT / "evals" / "scenarios.jsonl") if not s.faults]
    expected: set[str] = set()
    with httpx.Client(timeout=10) as http:
        for s in scenarios:
            event = build_event(s)
            event = event.model_copy(
                update={
                    "shipment_id": f"{event.shipment_id}-{run}",
                    "carrier_event_id": f"{event.carrier_event_id}-{run}",
                }
            )
            fixture = build_carrier(s, event).fixtures[event.shipment_id]
            http.put(
                f"{args.gateway}/v1/_admin/shipments/{event.shipment_id}",
                json=fixture.model_dump(mode="json"),
            ).raise_for_status()
            for _ in range(2):
                http.post(
                    f"{args.ingest}/v1/shipment-events", json=event.model_dump(mode="json")
                ).raise_for_status()
            if route(event).route.value != "ignore":
                expected.add(event.shipment_id)
    print(f"posted {len(scenarios)} scenarios x2; expecting {len(expected)} notifications")

    deadline = time.monotonic() + args.timeout
    rows: list[tuple[str, str, str]] = []
    with psycopg.connect(args.database) as conn:
        while time.monotonic() < deadline:
            rows = conn.execute(
                "SELECT shipment_id, channel, body FROM notification_outbox "
                "WHERE shipment_id LIKE %s",
                (f"%-{run}",),
            ).fetchall()
            if len(rows) >= len(expected):
                break
            time.sleep(2)

    got = Counter(r[0] for r in rows)
    duplicates = [sid for sid, n in got.items() if n > 1]
    missing = expected - set(got)
    print(f"notifications: {len(rows)} ({dict(Counter(r[1] for r in rows))})")
    for sid, channel, body in rows[:5]:
        print(f"  {sid:<45} {channel:<8} {body[:80]}")
    print("traces: http://localhost:16686  metrics: http://localhost:9090")
    if missing or duplicates:
        print(f"FAIL missing={sorted(missing)} duplicates={duplicates}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
