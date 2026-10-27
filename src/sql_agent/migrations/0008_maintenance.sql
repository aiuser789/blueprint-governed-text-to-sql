-- 0008 maintenance — no superuser needed.
--
-- The aggregate refresh used to be a SECURITY DEFINER function. Under a
-- non-superuser migrator (Supabase, RDS, Cloud SQL) that function runs as
-- a role that FORCE ROW LEVEL SECURITY filters to nothing, so it could not
-- see the orders it summarises. The refresh now runs from the migration
-- runner inside a short maintenance transaction as the table owner
-- (sql_agent/migrations.py: _maintenance, refresh_aggregates).

DROP FUNCTION IF EXISTS private.refresh_aggregates();
