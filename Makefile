.PHONY: install test check fmt typecheck eval eval-live failures gateway-test ci up down smoke
install:
	uv venv && uv pip install -e ".[dev]" && cd services/carrier-gateway && npm ci
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
ci: check typecheck test eval failures gateway-test
up:
	docker compose up -d --build
down:
	docker compose down -v
smoke:
	uv run python scripts/smoke.py
