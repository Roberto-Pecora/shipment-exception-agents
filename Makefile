.PHONY: install test check fmt typecheck eval eval-live failures gateway-test console-test api-types ci up down smoke
install:
	uv venv && uv pip install -e ".[dev]" && cd services/carrier-gateway && npm ci && cd ../ops-console && npm ci
test:
	uv run pytest -q
check:
	uv run ruff check src tests evals scripts && uv run ruff format --check src tests evals scripts
fmt:
	uv run ruff format src tests evals scripts && uv run ruff check --fix src tests evals scripts
typecheck:
	uv run mypy src
eval:
	uv run python evals/run_eval.py --gate
eval-live:
	uv run python evals/run_eval.py --mode live --strategy both
failures:
	uv run python evals/failure_suite.py
gateway-test:
	cd services/carrier-gateway && npm run typecheck && npm test
console-test:
	cd services/ops-console && npm run check:api && npm run typecheck && npm test
api-types:  # after changing ops-api's models: regenerate the spec and the console's types
	uv run python scripts/export_openapi.py && cd services/ops-console && npm run gen:api
ci: check typecheck test eval failures gateway-test console-test
up:
	docker compose up -d --build
down:
	docker compose down -v
smoke:
	uv run python scripts/smoke.py
