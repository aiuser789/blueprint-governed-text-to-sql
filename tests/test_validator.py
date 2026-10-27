"""The SQL validator: hermetic, no database."""

from __future__ import annotations

import pytest

from sql_agent.validator import validate


def rules(sql, user):
    return {h.rule for h in validate(sql, user).hits}


@pytest.mark.parametrize("sql,rule", [
    ("DELETE FROM orders", "not_select"),
    ("UPDATE orders SET region = 'West'", "not_select"),
    ("INSERT INTO orders (order_id) VALUES (1)", "not_select"),
    ("DROP TABLE orders", "not_select"),
    ("CREATE TABLE x (a int)", "not_select"),
    ("SET app.session_token = 'x'", "not_select"),
    ("SET ROLE nl2sql_owner", "not_select"),
    ("BEGIN", "not_select"),
    ("COPY (SELECT 1) TO STDOUT", "not_select"),
    ("EXPLAIN SELECT 1", "not_select"),
    ("SELECT order_id INTO TEMP t FROM orders", "not_select"),
    ("SELECT order_id FROM orders FOR UPDATE", "not_select"),
    ("WITH x AS (DELETE FROM orders RETURNING order_id) SELECT order_id FROM x", "not_select"),
    ("SELECT 1; DROP TABLE orders", "multiple_statements"),
    ("SELECT 1 /* ; */; DELETE FROM orders", "multiple_statements"),
    ("SELECT full_name FROM core.customers", "schema_not_allowed"),
    ("SELECT token FROM private.sessions", "schema_not_allowed"),
    ("SELECT usename FROM pg_catalog.pg_user", "schema_not_allowed"),
    ("SELECT table_name FROM information_schema.tables", "schema_not_allowed"),
    ("SELECT a FROM somewhere_else", "table_not_allowed"),
    ("SELECT pg_sleep(10)", "function_not_allowed"),
    ("SELECT set_config('app.session_token', 'x', true)", "function_not_allowed"),
    ("SELECT current_setting('app.session_token')", "function_not_allowed"),
    ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
    ("SELECT n FROM generate_series(1, 1000000000) n", "function_not_allowed"),
    ("SELECT dblink('host=x', 'select 1')", "function_not_allowed"),
    ("SELECT * FROM orders", "select_star"),
    ("SELECT o.* FROM orders o", "select_star"),
    ("SELECT (1", "parse_error"),
    ("SELEC broken", "not_select"),
    ("", "parse_error"),
])
def test_rejections(sql, rule, users):
    assert rule in rules(sql, users["sam"])


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM orders",
    "SELECT region, ROUND(SUM(total_amount), 2) AS revenue FROM orders WHERE status <> 'cancelled' AND order_date >= DATE '2026-08-01' GROUP BY region",
    "SELECT date_trunc('month', order_date)::date AS m, COUNT(*) AS n FROM orders GROUP BY 1 ORDER BY 1",
    "WITH t AS (SELECT customer_id, SUM(total_amount) AS r FROM orders GROUP BY customer_id) SELECT customer_id, r FROM t ORDER BY r DESC LIMIT 5",
    "SELECT p.category, RANK() OVER (ORDER BY SUM(i.quantity) DESC) AS rk FROM order_items i JOIN products p ON p.product_id = i.product_id GROUP BY p.category",
    "SELECT status, COUNT(*) FILTER (WHERE status = 'cancelled') AS c FROM orders GROUP BY status",
    "SELECT customer_id FROM orders WHERE customer_id IN (SELECT customer_id FROM customers WHERE segment = 'Enterprise')",
    "SELECT EXTRACT(YEAR FROM order_date) AS y, COUNT(*) AS n FROM orders GROUP BY 1",
    "SELECT 1 AS a UNION SELECT 2 AS a",
])
def test_legal_queries_pass(sql, users):
    v = validate(sql, users["sam"])
    assert v.ok, v.feedback()
    assert "LIMIT" in v.sql.upper()


def test_role_trimmed_tables(users):
    assert "table_not_allowed" in rules("SELECT order_id FROM orders", users["tom"])
    assert "table_not_allowed" in rules("SELECT customer_id FROM customers", users["tom"])
    assert validate("SELECT region, revenue FROM region_month_summary", users["tom"]).ok
    assert "table_not_allowed" in rules("SELECT region FROM region_month_summary", users["dev"])


def test_pii_filters_depend_on_tier(users):
    q = "SELECT customer_id FROM customers WHERE full_name LIKE 'A%'"
    assert "pii_in_filter" in rules(q, users["sam"])        # masked
    assert "pii_in_filter" in rules(q, users["dev"])        # partial: name is only partly visible
    assert validate(q, users["lena"]).ok                    # full
    city = "SELECT customer_id FROM customers WHERE city = 'Boston'"
    assert "pii_in_filter" in rules(city, users["sam"])
    assert validate(city, users["dev"]).ok                  # partial tier sees city in full
    assert "pii_in_filter" in rules("SELECT customer_id FROM customers ORDER BY email", users["sam"])
    assert "pii_in_filter" in rules("SELECT o.order_id FROM orders o JOIN customers c ON c.email = c.email", users["sam"])
    assert "pii_distinct_count" in rules("SELECT COUNT(DISTINCT email) FROM customers", users["sam"])


def test_selecting_masked_columns_is_allowed(users):
    # The database masks them; grouping by a masked name is grouping by a pseudonym.
    v = validate("SELECT c.customer_id, c.full_name, SUM(o.total_amount) AS r FROM orders o JOIN customers c ON c.customer_id = o.customer_id GROUP BY c.customer_id, c.full_name", users["priya"])
    assert v.ok, v.feedback()


def test_row_cap_applied_and_kept(users):
    assert validate("SELECT order_id FROM orders", users["sam"], max_rows=1000).sql.rstrip().endswith("LIMIT 1001")
    assert validate("SELECT order_id FROM orders LIMIT 5", users["sam"], max_rows=1000).sql.rstrip().endswith("LIMIT 5")
    assert validate("SELECT order_id FROM orders LIMIT 99999", users["sam"], max_rows=1000).sql.rstrip().endswith("LIMIT 1001")
    union = validate("SELECT 1 AS a UNION SELECT 2 AS a", users["sam"], max_rows=10).sql
    assert "capped" in union and union.rstrip().endswith("LIMIT 11")


def test_regenerated_sql_is_what_runs(users):
    # Comments and odd spacing never reach PostgreSQL: the checked tree is re-rendered.
    v = validate("SELECT /* hi */ order_id   FROM orders -- trailing", users["sam"])
    assert v.ok and "/*" not in v.sql and "--" not in v.sql
