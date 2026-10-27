-- 0005 grants — least privilege for the role that runs model-written SQL.
--
-- nl2sql_agent (LOGIN, NOBYPASSRLS, not a superuser) may:
--   * USAGE on schema analytics, SELECT on its views
--   * EXECUTE ctx.*() (the views and policies call them; they only ever
--     reveal the caller's OWN identity)
-- and nothing else: no core.*, no private.*, no CREATE anywhere, no
-- temporary tables, read-only transactions by default, a statement
-- timeout, and search_path pinned to analytics.
--
-- The role itself (and its password) is created by the migration runner
-- (sql_agent/migrations.py) before 0001, because a password does not
-- belong in a checked-in SQL file.

REVOKE ALL ON SCHEMA core FROM PUBLIC;
REVOKE ALL ON SCHEMA private FROM PUBLIC;
REVOKE ALL ON SCHEMA analytics FROM PUBLIC;
REVOKE ALL ON SCHEMA ctx FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA core FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA private FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA analytics FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA ctx FROM PUBLIC;
REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA private FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA ctx REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA private REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- The view owner evaluates policies and masking through ctx.*().
GRANT USAGE ON SCHEMA ctx TO nl2sql_owner, nl2sql_agent;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ctx TO nl2sql_owner, nl2sql_agent;
GRANT USAGE ON SCHEMA core TO nl2sql_owner;

-- The agent: the analytics views, nothing more.
GRANT USAGE ON SCHEMA analytics TO nl2sql_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO nl2sql_agent;

ALTER ROLE nl2sql_agent SET search_path = analytics;
ALTER ROLE nl2sql_agent SET default_transaction_read_only = on;
ALTER ROLE nl2sql_agent SET statement_timeout = '15s';
ALTER ROLE nl2sql_agent SET idle_in_transaction_session_timeout = '30s';
