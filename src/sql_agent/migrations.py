"""Migration runner: roles, numbered SQL files, demo users, dataset.

Runs as the SERVICE role (``DATABASE_URL``). Steps, all idempotent:

1. Create the two database roles if missing — ``nl2sql_owner`` (NOLOGIN,
   owns the base tables and views, subject to RLS) and ``nl2sql_agent``
   (LOGIN, runs model-written SQL) — and check that neither can bypass
   RLS. The agent password comes from ``AGENT_DB_PASSWORD``; it is (re)set
   on every run so rotating it is one env change.
2. Apply ``migrations/NNNN_*.sql`` in order, recording each in
   ``private.schema_migrations``.
3. Upsert the synthetic demo users (``data/users.json``).
4. Load the synthetic retail data if ``core.customers`` is empty, then
   refresh the minimum-group-size aggregates.

The service role does NOT need to be a superuser. It needs CREATEROLE
and CREATE on the database — what managed services such as Supabase,
RDS and Cloud SQL give their admin user. Bulk loading into the
RLS-protected tables happens in a short maintenance transaction as the
table owner (see ``_maintenance``), so BYPASSRLS is not needed either.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from importlib import resources
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from .config import AGENT_ROLE, DEFAULT_AGENT_PASSWORD, OWNER_ROLE, Settings
from .domains.retail.warehouse import REFRESH_AGGREGATES_SQL, TABLES as CORE_TABLES  # retail data tables in core.*

log = logging.getLogger(__name__)


def _migration_files() -> list[tuple[str, str]]:
    folder = resources.files("sql_agent") / "migrations"
    files = sorted(p for p in folder.iterdir() if p.name.endswith(".sql"))
    return [(p.name, p.read_text(encoding="utf-8")) for p in files]


def load_users() -> list[dict]:
    text = (resources.files("sql_agent") / "data" / "users.json").read_text(encoding="utf-8")
    return json.loads(text)["users"]


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "host.docker.internal"}


def is_local_database(url: str) -> bool:
    host = (urlsplit(url).hostname or "localhost").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")


def _ensure_roles(conn: psycopg.Connection, agent_password: str) -> None:
    with conn.cursor() as cur:
        for role, login in ((OWNER_ROLE, False), (AGENT_ROLE, True)):
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
            if cur.fetchone() is None:
                cur.execute(
                    sql.SQL("CREATE ROLE {} {} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOINHERIT").format(
                        sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN")
                    )
                )
        # Only the password and LOGIN are (re)set here: a non-superuser may not
        # even mention SUPERUSER in ALTER ROLE. The attributes that matter are
        # verified instead, below.
        cur.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(sql.Identifier(AGENT_ROLE), sql.Literal(agent_password)))
        cur.execute(sql.SQL("ALTER ROLE {} WITH NOLOGIN").format(sql.Identifier(OWNER_ROLE)))
        cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN (%s, %s) AND (rolsuper OR rolbypassrls)", (OWNER_ROLE, AGENT_ROLE))
        unsafe = [r[0] for r in cur.fetchall()]
        if unsafe:
            raise RuntimeError(f"Role(s) {', '.join(unsafe)} can bypass row-level security (SUPERUSER or BYPASSRLS). "
                               "Remove that attribute — the whole design depends on RLS applying to them.")
        # The migrator must be able to act as the owner role: to hand it the
        # tables (ALTER … OWNER TO) and to run the maintenance transaction.
        cur.execute("SELECT pg_has_role(current_user, %s, 'SET')", (OWNER_ROLE,))
        if not cur.fetchone()[0]:
            cur.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(OWNER_ROLE)))
        # No temporary tables for PUBLIC in this database. On a managed service
        # where the migrator does not own the database these are no-ops with a
        # warning; agent transactions are read-only anyway, so TEMP is refused.
        cur.execute("SELECT current_database()")
        dbname = cur.fetchone()[0]
        cur.execute(sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(dbname)))
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(dbname), sql.Identifier(AGENT_ROLE)))


@contextmanager
def _maintenance(conn: psycopg.Connection):
    """Bulk work on core.* as the table owner, inside ONE transaction.

    The core tables use FORCE ROW LEVEL SECURITY, so even their owner sees
    nothing without a session — including the loader. Inside this
    transaction the owner lifts FORCE, does the work, and puts FORCE back
    before committing. ALTER TABLE holds an ACCESS EXCLUSIVE lock until the
    commit, and DDL is transactional, so no other session can ever read a
    table in the lifted state; on any error the rollback restores it.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
        for t in CORE_TABLES:
            cur.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(sql.Identifier("core", t)))
    try:
        yield
        with conn.cursor() as cur:
            for t in CORE_TABLES:
                cur.execute(sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(sql.Identifier("core", t)))
            cur.execute("RESET ROLE")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


@contextmanager
def raw_read(conn: psycopg.Connection):
    """Read core.* unfiltered (for the eval leak checks), then roll back.

    Same mechanism as ``_maintenance`` but never committed: the lifted
    FORCE exists only inside this transaction and is undone on exit.
    """
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(OWNER_ROLE)))
        for t in CORE_TABLES:
            cur.execute(sql.SQL("ALTER TABLE {} NO FORCE ROW LEVEL SECURITY").format(sql.Identifier("core", t)))
    try:
        yield
    finally:
        conn.rollback()




def refresh_aggregates(conn: psycopg.Connection) -> None:
    """Rebuild the minimum-group-size summaries the contractor role reads."""
    with _maintenance(conn), conn.cursor() as cur:
        cur.execute(REFRESH_AGGREGATES_SQL)


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    applied: list[str] = []
    with conn.cursor() as cur:
        # The owner role must hold CREATE on a schema before a non-superuser
        # migrator can give it tables or views there (ALTER … OWNER TO).
        for schema in ("core", "private", "analytics", "ctx"):
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("GRANT USAGE, CREATE ON SCHEMA core, analytics TO {}").format(sql.Identifier(OWNER_ROLE)))
        cur.execute(
            "CREATE TABLE IF NOT EXISTS private.schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT name FROM private.schema_migrations")
        done = {row[0] for row in cur.fetchall()}
        for name, body in _migration_files():
            if name in done:
                continue
            log.info("applying migration %s", name)
            cur.execute(body)
            cur.execute("INSERT INTO private.schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
        # Grants are re-applied every run so a view added later is covered.
        cur.execute("GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO nl2sql_agent")
        cur.execute("REVOKE ALL ON ALL TABLES IN SCHEMA private FROM PUBLIC")
    return applied


def upsert_users(conn: psycopg.Connection, users: list[dict]) -> None:
    with conn.cursor() as cur:
        for u in users:
            cur.execute(
                "INSERT INTO private.app_users (user_id, display_name, role, region, rep_id, pii_tier, can_approve)"
                " VALUES (%(user_id)s, %(display_name)s, %(role)s, %(region)s, %(rep_id)s, %(pii_tier)s, %(can_approve)s)"
                " ON CONFLICT (user_id) DO UPDATE SET display_name = EXCLUDED.display_name, role = EXCLUDED.role,"
                " region = EXCLUDED.region, rep_id = EXCLUDED.rep_id, pii_tier = EXCLUDED.pii_tier,"
                " can_approve = EXCLUDED.can_approve",
                {"region": None, "rep_id": None, "can_approve": False, **u},
            )


def migrate(settings: Settings, *, load_data: bool = True) -> dict:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set — this blueprint needs PostgreSQL (see README Quickstart).")
    from .domains.retail import seed  # local import: the retail data is only loaded here

    if not is_local_database(settings.database_url) and (
            settings.agent_db_password == DEFAULT_AGENT_PASSWORD or len(settings.agent_db_password) < 16):
        raise RuntimeError(
            "This database is not on your machine, so the agent role needs a real password: set AGENT_DB_PASSWORD "
            "in .env to a random string of 16+ characters (for example: python -c \"import secrets; print(secrets.token_urlsafe(24))\").")

    with psycopg.connect(settings.database_url, autocommit=False, connect_timeout=15) as conn:
        _ensure_roles(conn, settings.agent_db_password)
        applied = apply_migrations(conn)
        conn.commit()
        upsert_users(conn, load_users())
        conn.commit()
        loaded = False
        if load_data:
            with _maintenance(conn), conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM core.customers")
                if cur.fetchone()[0] == 0:
                    seed.load(conn)
                    loaded = True
            refresh_aggregates(conn)
    return {"applied": applied, "retail_loaded": loaded}
