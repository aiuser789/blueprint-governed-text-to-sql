"""Executor — runs validated SQL as the asker, inside PostgreSQL's rules.

Per question:

1. Open a FRESH agent connection (role ``nl2sql_agent``) and read its
   backend PID. A fresh connection per question means no session state,
   prepared statement or setting can carry from one user to the next.
2. On the SERVICE connection, mint a session: a random 128-bit token,
   the asker's user id, that PID, and a short expiry.
3. On the agent connection: ``BEGIN READ ONLY``, ``SET LOCAL
   statement_timeout``, ``set_config('app.session_token', token, true)``
   (transaction-local), run the SQL as a server-side prepared statement
   (so PostgreSQL rejects stacked statements on its own), fetch at most
   ``max_rows + 1`` rows, ``ROLLBACK``.
4. Delete the session token, whatever happened.

The model-written SQL never runs on the service connection, and the
agent connection can neither read nor write ``private.*`` — it cannot
mint, list or extend a session.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row

from .config import Settings


class ExecutionError(Exception):
    """The database refused or failed the query. Safe to show the model."""

    def __init__(self, message: str, sqlstate: str | None = None):
        super().__init__(message)
        self.sqlstate = sqlstate


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    duration_ms: int
    backend_pid: int = 0
    raw_rows: list[tuple] = field(default_factory=list, repr=False)


def to_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def format_timestamp(value: datetime) -> str:
    """Readable timestamps: a midnight value (e.g. date_trunc('month', …)) is shown as its date,
    2026-07-01; otherwise 2026-07-01 14:30:00, with ' UTC' or the offset for time-zone-aware values."""
    if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
        return value.date().isoformat()
    text = value.strftime("%Y-%m-%d %H:%M:%S")
    offset = value.utcoffset()
    if offset is None:
        return text
    if not offset:
        return text + " UTC"
    return text + " " + value.strftime("%z")[:3] + ":" + value.strftime("%z")[3:]


class Executor:
    def __init__(self, settings: Settings):
        if not settings.database_url or not settings.agent_database_url:
            raise RuntimeError("DATABASE_URL is not set — this blueprint needs PostgreSQL (see README Quickstart).")
        self.settings = settings

    # -- service-role helpers ------------------------------------------------
    def service(self) -> psycopg.Connection:
        return psycopg.connect(self.settings.database_url, autocommit=True)

    def _mint(self, svc: psycopg.Connection, user_id: str, pid: int) -> str:
        token = secrets.token_hex(16)
        svc.execute(
            "INSERT INTO private.sessions (token, user_id, backend_pid, expires_at)"
            " VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (token, user_id, pid, self.settings.session_ttl_seconds),
        )
        return token

    def _revoke(self, svc: psycopg.Connection, token: str) -> None:
        svc.execute("DELETE FROM private.sessions WHERE token = %s OR expires_at < now()", (token,))

    # -- the one way model-written SQL reaches the database ----------------------
    def run(self, user_id: str, sql: str, *, max_rows: int, timeout_ms: int | None = None) -> ExecResult:
        timeout_ms = timeout_ms or self.settings.statement_timeout_ms
        started = time.perf_counter()
        with self.service() as svc, psycopg.connect(self.settings.agent_database_url, autocommit=True) as agent:
            pid = agent.execute("SELECT pg_backend_pid()").fetchone()[0]
            token = self._mint(svc, user_id, pid)
            try:
                with agent.cursor() as cur:
                    cur.execute("BEGIN READ ONLY")
                    try:
                        cur.execute(pgsql.SQL("SET LOCAL statement_timeout = {}").format(pgsql.Literal(int(timeout_ms))))
                        cur.execute("SELECT set_config('app.session_token', %s, true)", (token,))
                        try:
                            # prepare=True sends the SQL as a server-side prepared statement, so
                            # PostgreSQL itself refuses more than one statement.
                            cur.execute(sql, prepare=True)
                        except psycopg.Error as e:
                            diag = getattr(e, "diag", None)
                            message = (diag.message_primary if diag and diag.message_primary else str(e)).splitlines()[0]
                            raise ExecutionError(message, getattr(e, "sqlstate", None)) from None
                        if cur.description is None:
                            raise ExecutionError("The statement returned no result set.")
                        columns = [d.name for d in cur.description]
                        rows = cur.fetchmany(max_rows + 1)
                    finally:
                        try:
                            cur.execute("ROLLBACK")
                        except psycopg.Error:
                            # The connection is in a state ROLLBACK can't reach (e.g. a COPY
                            # protocol switch). It is per-question and discarded anyway.
                            agent.close()
            finally:
                self._revoke(svc, token)
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        return ExecResult(
            columns=columns,
            rows=[tuple(to_json_value(v) for v in r) for r in rows],
            truncated=truncated,
            duration_ms=int((time.perf_counter() - started) * 1000),
            backend_pid=pid,
            raw_rows=rows,
        )

    # -- service-role reads used by the app (never with model SQL) ----------------
    def load_users(self) -> list[dict]:
        with self.service() as svc, svc.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT user_id, display_name, role, region, rep_id, pii_tier, can_approve FROM private.app_users ORDER BY user_id")
            return list(cur.fetchall())

    def ping(self) -> dict:
        with self.service() as svc:
            version = svc.execute("SHOW server_version").fetchone()[0]
            migrations = [r[0] for r in svc.execute("SELECT name FROM private.schema_migrations ORDER BY name").fetchall()]
            # Table statistics, not count(*): unless it is a superuser the service
            # role is itself filtered by RLS, and a lock-free estimate suits a probe.
            customers = svc.execute(
                "SELECT coalesce((SELECT n_live_tup FROM pg_stat_user_tables WHERE schemaname = 'core' AND relname = 'customers'), 0)"
            ).fetchone()[0]
        with psycopg.connect(self.settings.agent_database_url, autocommit=True) as agent:
            agent_role = agent.execute("SELECT current_user").fetchone()[0]
        return {"server_version": version, "migrations": migrations, "customers": customers, "agent_role": agent_role}
