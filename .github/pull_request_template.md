## What and why

<!-- One paragraph: the problem, the change, and how it was verified. -->

## Review checklist

- [ ] `make ci` passes locally (lint, mypy --strict, tests, offline eval gate, failure drills, gateway tests)
- [ ] New or changed behaviour has a test; agent behaviour changes have an eval scenario
- [ ] Contracts: event or API schema changes are versioned (new topic / `schema_version`), not edited in place
- [ ] Delivery: new consumers are idempotent; side effects carry an idempotency key
- [ ] Agent: new tools have a pydantic args model, a guardrail decision, and a scenario
- [ ] Guardrails or routing thresholds changed → ADR updated and reviewer from ops tagged
- [ ] Telemetry: new hops propagate `traceparent`; new failure modes have a metric
- [ ] No secrets, PII or prompts in logs or span attributes
