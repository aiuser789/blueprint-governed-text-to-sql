"""Hosted PostgreSQL (Supabase, RDS, Cloud SQL): connection strings and safety rules."""

from __future__ import annotations

import psycopg
import pytest

from sql_agent.config import derive_agent_url, get_settings
from sql_agent.migrations import CORE_TABLES, is_local_database, migrate

SUPABASE = "postgresql://postgres.isebztyyzdawgsltoepn:svc-pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"


def test_agent_url_keeps_the_pooler_suffix_and_query():
    url = derive_agent_url(SUPABASE, "p@ss/w:rd")
    assert url == ("postgresql://nl2sql_agent.isebztyyzdawgsltoepn:p%40ss%2Fw%3Ard"
                   "@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require")


def test_agent_url_plain_host_unchanged():
    assert derive_agent_url("postgresql://nl2sql:x@localhost:5432/nl2sql", "pw") == "postgresql://nl2sql_agent:pw@localhost:5432/nl2sql"


@pytest.mark.parametrize("url,local", [
    ("postgresql://u@localhost:5432/db", True),
    ("postgresql://u@127.0.0.1/db", True),
    ("postgresql://u@postgres:5432/db", True),
    (SUPABASE, False),
    ("postgresql://u@mydb.abc123.us-east-1.rds.amazonaws.com/db", False),
])
def test_is_local_database(url, local):
    assert is_local_database(url) is local


@pytest.mark.parametrize("password", ["agent-local-only", "short"])
def test_remote_database_refuses_weak_agent_password(monkeypatch, password):
    monkeypatch.setenv("DATABASE_URL", SUPABASE)
    monkeypatch.setenv("AGENT_DB_PASSWORD", password)
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="AGENT_DB_PASSWORD"):
        migrate(get_settings())  # refused before any connection is attempted


@pytest.mark.db
def test_rls_is_forced_after_migrate(migrated):
    with psycopg.connect(migrated.database_url) as conn:
        rows = conn.execute(
            "SELECT relname, relforcerowsecurity, pg_get_userbyid(relowner) FROM pg_class "
            "WHERE relnamespace = 'core'::regnamespace AND relkind = 'r'").fetchall()
    assert {r[0] for r in rows} == set(CORE_TABLES)
    assert all(r[1] for r in rows), rows
    assert {r[2] for r in rows} == {"nl2sql_owner"}


@pytest.mark.db
def test_migrator_cannot_see_rows_outside_maintenance(migrated):
    """Unless it is a superuser, the service role is filtered by RLS like anyone else."""
    with psycopg.connect(migrated.database_url) as conn:
        is_super = conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user").fetchone()[0]
        count = conn.execute("SELECT count(*) FROM core.orders").fetchone()[0]
    assert count == (50000 if is_super else 0)
