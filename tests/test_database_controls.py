"""The database is the boundary: RLS, masking, grants and session binding.

These tests talk to PostgreSQL directly as the agent role, with no
validator in front, so they prove what holds if every Python-side check
were removed.
"""

from __future__ import annotations

import secrets

import psycopg
import pytest

from sql_agent.executor import ExecutionError, Executor

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def ex(migrated):
    return Executor(migrated)


def run(ex, user, sql, **kw):
    return ex.run(user, sql, max_rows=kw.pop("max_rows", 10000), **kw)


def test_supervisor_officer_sees_only_their_region(ex):
    res = run(ex, "priya", "SELECT DISTINCT region FROM orders")
    assert [r[0] for r in res.rows] == ["West"]
    res = run(ex, "priya", "SELECT DISTINCT region FROM customers")
    assert [r[0] for r in res.rows] == ["West"]


def test_sales_rep_sees_only_assigned_customers(ex):
    reps = run(ex, "dev", "SELECT DISTINCT assigned_rep_id FROM customers")
    assert [r[0] for r in reps.rows] == ["R-E-03"]
    orphan = run(ex, "dev", "SELECT COUNT(*) FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.customer_id = o.customer_id)")
    assert orphan.rows[0][0] == 0


def test_contractor_sees_no_rows_only_aggregates(ex):
    for view in ("customers", "orders", "order_items", "sales_reps"):
        assert run(ex, "tom", f"SELECT COUNT(*) FROM {view}").rows[0][0] == 0
    assert run(ex, "tom", "SELECT COUNT(*) FROM region_month_summary").rows[0][0] > 0
    assert run(ex, "tom", "SELECT COUNT(*) FROM region_month_summary WHERE orders < 10").rows[0][0] == 0


def test_masking_per_tier(ex):
    sql = "SELECT full_name, email, phone, street_address, city, postal_code, date_of_birth FROM customers ORDER BY customer_id LIMIT 1"
    masked = run(ex, "sam", sql).rows[0]
    assert masked[0].startswith("Customer #") and set(masked[1:]) == {"[REDACTED]"}
    partial = run(ex, "dev", sql).rows[0]
    assert partial[0].endswith(".") and partial[1].startswith(partial[1][0] + "***@") and partial[2].startswith("***-***-")
    assert partial[3] == "[REDACTED]" and partial[5].endswith("**") and len(partial[6]) == 4
    full = run(ex, "lena", sql).rows[0]
    assert "@" in full[1] and "***" not in full[1] and full[3] != "[REDACTED]"


def test_filters_run_against_masked_values(ex):
    # The probe a validator bug would let through: still nothing real to match.
    assert run(ex, "sam", "SELECT COUNT(*) FROM customers WHERE full_name LIKE 'A%'").rows[0][0] == 0
    assert run(ex, "sam", "SELECT COUNT(*) FROM customers WHERE email LIKE '%@example.com'").rows[0][0] == 0
    assert run(ex, "lena", "SELECT COUNT(*) FROM customers WHERE email LIKE '%@example.com'").rows[0][0] == 5000


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM core.customers",
    "SELECT COUNT(*) FROM private.sessions",
    "SELECT COUNT(*) FROM private.audit_log",
    "SELECT COUNT(*) FROM private.app_users",
    "DELETE FROM orders",
    "UPDATE customers SET region = 'West'",
    "INSERT INTO products (product_id, name, category, list_price) VALUES (1, 'x', 'y', 1)",
    "CREATE TABLE analytics.x (a int)",
    "CREATE TEMP TABLE x (a int)",
    "SELECT pg_read_file('/etc/passwd')",
    "SET ROLE nl2sql_owner",
])
def test_agent_role_privileges(ex, sql):
    with pytest.raises(ExecutionError):
        run(ex, "lena", sql)


def test_timeout(ex):
    with pytest.raises(ExecutionError) as err:
        run(ex, "sam", "SELECT pg_sleep(3)", timeout_ms=300)
    assert err.value.sqlstate == "57014"


def test_no_session_means_no_rows(migrated):
    with psycopg.connect(migrated.agent_database_url, autocommit=True) as agent:
        assert agent.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        assert agent.execute("SELECT full_name FROM customers LIMIT 1").fetchone() is None


def test_token_is_bound_to_its_connection(migrated):
    """A valid token minted for one connection is useless on another."""
    with psycopg.connect(migrated.database_url, autocommit=True) as svc, \
            psycopg.connect(migrated.agent_database_url, autocommit=True) as a, \
            psycopg.connect(migrated.agent_database_url, autocommit=True) as b:
        pid_a = a.execute("SELECT pg_backend_pid()").fetchone()[0]
        token = secrets.token_hex(16)
        svc.execute("INSERT INTO private.sessions (token, user_id, backend_pid, expires_at) VALUES (%s, 'lena', %s, now() + interval '60 seconds')",
                    (token, pid_a))
        try:
            with a.transaction():
                a.execute("SELECT set_config('app.session_token', %s, true)", (token,))
                assert a.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 50000
            with b.transaction():  # stolen token, other backend
                b.execute("SELECT set_config('app.session_token', %s, true)", (token,))
                assert b.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        finally:
            svc.execute("DELETE FROM private.sessions WHERE token = %s", (token,))


def test_expired_and_revoked_tokens(migrated):
    with psycopg.connect(migrated.database_url, autocommit=True) as svc, \
            psycopg.connect(migrated.agent_database_url, autocommit=True) as a:
        pid = a.execute("SELECT pg_backend_pid()").fetchone()[0]
        token = secrets.token_hex(16)
        svc.execute("INSERT INTO private.sessions (token, user_id, backend_pid, expires_at) VALUES (%s, 'lena', %s, now() - interval '1 second')",
                    (token, pid))
        with a.transaction():
            a.execute("SELECT set_config('app.session_token', %s, true)", (token,))
            assert a.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
        svc.execute("DELETE FROM private.sessions WHERE token = %s", (token,))


def test_executor_revokes_tokens(ex, migrated):
    run(ex, "sam", "SELECT 1")
    with psycopg.connect(migrated.database_url, autocommit=True) as svc:
        assert svc.execute("SELECT COUNT(*) FROM private.sessions WHERE expires_at > now()").fetchone()[0] == 0


def test_cannot_mint_a_session_from_agent_sql(ex):
    with pytest.raises(ExecutionError):
        run(ex, "priya", "INSERT INTO private.sessions (token, user_id, backend_pid, expires_at) VALUES ('t', 'lena', pg_backend_pid(), now() + interval '1 hour')")


def test_overwriting_the_token_mid_query_reveals_nothing(ex):
    res = run(ex, "priya", "SELECT set_config('app.session_token', 'guess', true) AS t, COUNT(*) AS n FROM orders")
    west = run(ex, "priya", "SELECT COUNT(*) FROM orders").rows[0][0]
    assert res.rows[0][1] in (0, west)


@pytest.mark.parametrize("sql", ["SELECT 1; SELECT 2", "SELECT 1 /* ; */; DELETE FROM orders", "SELECT $$;$$ AS a; SELECT 1"])
def test_database_refuses_stacked_statements(ex, sql):
    with pytest.raises(ExecutionError) as err:
        run(ex, "lena", sql)
    assert "multiple commands" in str(err.value)


@pytest.mark.db
def test_role_is_renamed_in_the_database(migrated):
    import psycopg

    with psycopg.connect(migrated.database_url) as c:
        roles = {r[0] for r in c.execute("SELECT DISTINCT role FROM private.app_users")}
        policies = " ".join(r[0] for r in c.execute("SELECT qual FROM pg_policies WHERE schemaname = 'core'"))
        checks = " ".join(r[0] for r in c.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'private.app_users'::regclass AND contype = 'c'"))
    assert "supervisor_officer" in roles and "regional_manager" not in roles
    assert "supervisor_officer" in policies and "regional_manager" not in policies
    assert "supervisor_officer" in checks and "regional_manager" not in checks
