"""Configuration: environment variables, with an optional ``.env``.

Real environment variables always win over ``.env``. Every knob has a
default that keeps the offline path reproducible (``AS_OF_DATE`` pins
"today" to the last day of the synthetic data).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

AGENT_ROLE = "nl2sql_agent"
OWNER_ROLE = "nl2sql_owner"
DEFAULT_AGENT_PASSWORD = "agent-local-only"


PASSWORD_FILE = ".password.env"


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env, then from .password.env beside it, without
    overriding the real environment. .password.env keeps the database password
    (DATABASE_PASSWORD=...) out of .env, so .env can be shared or shown safely."""
    if path is None:
        _load_env_file(Path.cwd() / ".env")
        _load_env_file(Path.cwd() / PASSWORD_FILE)
    else:
        _load_env_file(path)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "on", "true", "yes"}


def derive_agent_url(service_url: str, password: str) -> str:
    """Swap the service credentials in a URL for the agent role's.

    Connection poolers that route by user name (Supabase's Supavisor,
    ``postgres.<project-ref>``) need the same suffix on every role, so a
    ``.<suffix>`` on the service user is carried over to the agent user.
    The query string (e.g. ``sslmode=require``) is kept.
    """
    parts = urlsplit(service_url)
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    service_user = unquote(parts.username or "")
    suffix = service_user[service_user.index("."):] if "." in service_user else ""
    netloc = f"{quote(AGENT_ROLE + suffix, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    agent_database_url: str | None
    agent_db_password: str
    model_backend: str
    as_of_date: date
    fiscal_year_start_month: int
    statement_timeout_ms: int
    max_result_rows: int
    export_max_rows: int
    max_sql_attempts: int
    session_ttl_seconds: int
    api_key: str | None
    demo_identity: bool
    llm_timeout_seconds: int
    llm_max_tokens: int
    extra: dict = field(default_factory=dict)

    @property
    def backend_label(self) -> str:
        return self.model_backend or "mock"


def with_password(url: str, password: str) -> str:
    """Put a password into a postgres URL that has none (URL-encoded; an existing password wins)."""
    parts = urlsplit(url)
    if not password or parts.password is not None or "@" not in parts.netloc:
        return url
    userinfo, host = parts.netloc.rsplit("@", 1)
    return urlunsplit(parts._replace(netloc=f"{userinfo}:{quote(password, safe='')}@{host}"))


def get_settings() -> Settings:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL", "").strip() or None
    if database_url:  # DATABASE_PASSWORD (e.g. from .password.env) fills in a password-less DATABASE_URL
        database_url = with_password(database_url, os.environ.get("DATABASE_PASSWORD", ""))
    password = os.environ.get("AGENT_DB_PASSWORD", "").strip() or DEFAULT_AGENT_PASSWORD
    agent_url = os.environ.get("AGENT_DATABASE_URL", "").strip() or None
    if database_url and not agent_url:
        agent_url = derive_agent_url(database_url, password)
    as_of = os.environ.get("AS_OF_DATE", "").strip() or "2026-09-30"
    return Settings(
        database_url=database_url,
        agent_database_url=agent_url,
        agent_db_password=password,
        model_backend=(os.environ.get("MODEL_BACKEND", "").strip().lower() or "mock"),
        as_of_date=date.today() if as_of.lower() == "today" else date.fromisoformat(as_of),
        fiscal_year_start_month=_int("FISCAL_YEAR_START_MONTH", 2),
        statement_timeout_ms=_int("STATEMENT_TIMEOUT_MS", 15000),
        max_result_rows=_int("MAX_RESULT_ROWS", 1000),
        export_max_rows=_int("EXPORT_MAX_ROWS", 50000),
        max_sql_attempts=max(1, min(_int("MAX_SQL_ATTEMPTS", 3), 5)),
        session_ttl_seconds=_int("SESSION_TTL_SECONDS", 60),
        api_key=os.environ.get("API_KEY", "").strip() or None,
        demo_identity=_flag("DEMO_IDENTITY", True),
        llm_timeout_seconds=_int("LLM_TIMEOUT_SECONDS", 60),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 2000),
    )
