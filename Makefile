.PHONY: install db-up db-down migrate ollama viewer demo test evals redteam llm-evals serve

install:  ## Install the locked set with uv (primary workflow)
	uv sync --extra dev

db-up:  ## Start only PostgreSQL from compose (published on localhost:5432)
	docker compose -f docker-compose.yml -f docker-compose.local.yml up -d postgres

db-down:  ## Stop the compose stack
	docker compose down

migrate:  ## Apply migrations and load the synthetic retail data (idempotent)
	uv run sql-agent migrate

ollama:  ## Download the default local model (Ollama must be installed and running)
	ollama pull $${OLLAMA_MODEL:-qwen2.5-coder:7b}

viewer:  ## Check the database and the model backend
	uv run sql-agent viewer

demo:  ## Traced demo: one question asked as three different users (configured backend)
	uv run sql-agent-demo

test:  ## Run the pytest suite (database tests skip without DATABASE_URL)
	uv run pytest -q

evals:  ## Golden set + red-team pack (gate: zero leaks, accuracy >= 95%)
	uv run python evals/run_evals.py

redteam:  ## Red-team pack only: hostile SQL with and without the validator
	uv run python evals/run_evals.py --redteam-only

llm-evals:  ## Opt-in: golden set through the configured model (Ollama or a provider; NOT part of the default gate)
	uv run python evals/run_llm_evals.py

serve:  ## Serve the API + web console on http://localhost:8000
	uv run uvicorn sql_agent.api:app --port 8000
