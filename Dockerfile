# Governed Text-to-SQL Agent — production-shaped image.
#
# Multi-stage: the build stage resolves the locked dependency set with
# uv; the runtime stage is a slim Python image running as a non-root
# user. The container is STATELESS: users, sessions, the audit log,
# export requests and glossary proposals all live in PostgreSQL.
#
# NOTE: not built in the blueprint's dev sandbox (no Docker daemon
# there) — `docker compose up --build` is the first-run verification
# step, exactly as the README's QA checklist states.

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
ARG UV_EXTRA_ARGS="--extra llm"
RUN uv sync --frozen --no-dev $UV_EXTRA_ARGS

FROM python:3.12-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --home-dir /app app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src

ENV PYTHONPATH=/app/src \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

# Migrations + the synthetic retail data load at startup (idempotent), then serve.
CMD ["sh", "-c", "sql-agent migrate && uvicorn sql_agent.api:app --host 0.0.0.0 --port 8000"]
