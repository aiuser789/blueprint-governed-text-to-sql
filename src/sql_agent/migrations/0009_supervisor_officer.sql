-- 0009 rename the role regional_manager -> supervisor_officer.
--
-- Earlier migrations stay as they were (they already ran on existing
-- databases); this one moves an existing database to the new name:
-- the users table's checks, the users themselves, and every row-level
-- policy that scoped by role. Same scope as before: their region only.

-- 1. The users table: drop the checks that name the old role, rename, re-add.
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'private.app_users'::regclass AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%regional_manager%'
    LOOP
        EXECUTE format('ALTER TABLE private.app_users DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;

UPDATE private.app_users SET role = 'supervisor_officer' WHERE role = 'regional_manager';

ALTER TABLE private.app_users ADD CONSTRAINT app_users_role_check
    CHECK (role IN ('supervisor_officer', 'sales_rep', 'analyst', 'privacy_officer', 'contractor'));
ALTER TABLE private.app_users ADD CONSTRAINT app_users_supervisor_region_check
    CHECK (role <> 'supervisor_officer' OR region IS NOT NULL);

-- 2. Row-level policies (as in 0003, with the new role name).
DROP POLICY IF EXISTS customers_scope ON core.customers;
CREATE POLICY customers_scope ON core.customers FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer')
    OR ((SELECT ctx.role()) = 'supervisor_officer' AND region = (SELECT ctx.region()))
    OR ((SELECT ctx.role()) = 'sales_rep' AND assigned_rep_id = (SELECT ctx.rep_id()))
);

DROP POLICY IF EXISTS orders_scope ON core.orders;
CREATE POLICY orders_scope ON core.orders FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer')
    OR ((SELECT ctx.role()) = 'supervisor_officer' AND region = (SELECT ctx.region()))
    OR ((SELECT ctx.role()) = 'sales_rep' AND customer_id IN (
            SELECT c.customer_id FROM core.customers c
            WHERE c.assigned_rep_id = (SELECT ctx.rep_id())))
);

DROP POLICY IF EXISTS sales_reps_scope ON core.sales_reps;
CREATE POLICY sales_reps_scope ON core.sales_reps FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer')
    OR ((SELECT ctx.role()) = 'supervisor_officer' AND region = (SELECT ctx.region()))
    OR ((SELECT ctx.role()) = 'sales_rep' AND rep_id = (SELECT ctx.rep_id()))
);

DROP POLICY IF EXISTS region_month_agg_scope ON core.region_month_agg;
CREATE POLICY region_month_agg_scope ON core.region_month_agg FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer', 'contractor')
    OR ((SELECT ctx.role()) = 'supervisor_officer' AND region = (SELECT ctx.region()))
);

DROP POLICY IF EXISTS category_month_agg_scope ON core.category_month_agg;
CREATE POLICY category_month_agg_scope ON core.category_month_agg FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer', 'contractor')
    OR ((SELECT ctx.role()) = 'supervisor_officer' AND region = (SELECT ctx.region()))
);
