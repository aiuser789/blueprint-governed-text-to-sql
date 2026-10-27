# Contributing

This is a reference prototype. Contributions that make the core clearer, better tested, or easier to take and adapt to another warehouse are welcome.

## Dev setup

Primary workflow is [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev
make db-up                      # PostgreSQL from compose on localhost:5432 (or bring your own)
export DATABASE_URL=postgresql://nl2sql:nl2sql@localhost:5432/nl2sql
make migrate
```

No uv? `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`.

## Before opening a PR

Run all three and make sure they pass:

```bash
uv run pytest -q                  # full suite (database tests need DATABASE_URL)
uv run python evals/run_evals.py  # the gate: zero leaks, red-team 28/28, accuracy >= 95%
uv run sql-agent-demo             # end-to-end trace
```

(`make test`, `make evals` and `make demo` wrap the same commands.)

## Code style

- Python 3.10–3.14, typed with Pydantic models at the boundaries.
- **Access control lives in the database.** A rule about who may see which rows or columns belongs in a migration (RLS policy, masked view, grant) with a test in `tests/test_database_controls.py` that runs *without* the validator.
- Python-side rules (screens, validator, output scan) are for honesty and precise feedback; each one has a test, and a new attack gets a red-team case.
- Migrations are append-only and numbered, and live only in `src/sql_agent/migrations/`.
- Keep the frontend to one static HTML file, inline CSS / vanilla JS, no build step.

## Two hard rules

- **Synthetic data only.** Never commit real customer, employee or order data — not in samples, tests, evals or issue reports.
- **Honest claims only.** Eval numbers describe the synthetic golden set and are a regression gate, not a real-world benchmark — word them that way, and don't report offline-planner accuracy as model accuracy.
