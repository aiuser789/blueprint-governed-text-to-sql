-- 0003 row-level security — enforced by PostgreSQL, not by the prompt.
-- (The role regional_manager is renamed supervisor_officer by 0009.)
--
-- FORCE ROW LEVEL SECURITY makes the policies apply to the table owner
-- (nl2sql_owner) too. That matters because the analytics views run with
-- their owner's privileges: without FORCE, reading through a view would
-- skip every policy below.
--
-- Each ctx.*() call is wrapped in a scalar sub-select so PostgreSQL
-- evaluates it once per query (an InitPlan), not once per row.
--
-- Scope by role:
--   analyst, privacy_officer   every row
--   regional_manager           rows in their region
--   sales_rep                  customers assigned to them (and their orders)
--   contractor                 no rows at all — aggregates only (0004)
--   no valid session           no rows at all

ALTER TABLE core.customers ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.customers FORCE ROW LEVEL SECURITY;
ALTER TABLE core.orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.orders FORCE ROW LEVEL SECURITY;
ALTER TABLE core.order_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.order_items FORCE ROW LEVEL SECURITY;
ALTER TABLE core.sales_reps ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.sales_reps FORCE ROW LEVEL SECURITY;
ALTER TABLE core.region_month_agg ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.region_month_agg FORCE ROW LEVEL SECURITY;
ALTER TABLE core.category_month_agg ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.category_month_agg FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS customers_scope ON core.customers;
CREATE POLICY customers_scope ON core.customers FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer')
    OR ((SELECT ctx.role()) = 'regional_manager' AND region = (SELECT ctx.region()))
    OR ((SELECT ctx.role()) = 'sales_rep' AND assigned_rep_id = (SELECT ctx.rep_id()))
);

DROP POLICY IF EXISTS orders_scope ON core.orders;
CREATE POLICY orders_scope ON core.orders FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer')
    OR ((SELECT ctx.role()) = 'regional_manager' AND region = (SELECT ctx.region()))
    OR ((SELECT ctx.role()) = 'sales_rep' AND customer_id IN (
            SELECT c.customer_id FROM core.customers c
            WHERE c.assigned_rep_id = (SELECT ctx.rep_id())))
);

-- An order line is visible exactly when its order is (the sub-query is
-- itself filtered by orders_scope).
DROP POLICY IF EXISTS order_items_scope ON core.order_items;
CREATE POLICY order_items_scope ON core.order_items FOR SELECT
USING (
    order_id IN (SELECT o.order_id FROM core.orders o)
);

DROP POLICY IF EXISTS sales_reps_scope ON core.sales_reps;
CREATE POLICY sales_reps_scope ON core.sales_reps FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer')
    OR ((SELECT ctx.role()) = 'regional_manager' AND region = (SELECT ctx.region()))
    OR ((SELECT ctx.role()) = 'sales_rep' AND rep_id = (SELECT ctx.rep_id()))
);

-- Aggregates: everyone with a scope beyond single customers, but a
-- regional manager still sees only their region. Sales reps are scoped
-- to their own book, which these regional totals would exceed.
DROP POLICY IF EXISTS region_month_agg_scope ON core.region_month_agg;
CREATE POLICY region_month_agg_scope ON core.region_month_agg FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer', 'contractor')
    OR ((SELECT ctx.role()) = 'regional_manager' AND region = (SELECT ctx.region()))
);

DROP POLICY IF EXISTS category_month_agg_scope ON core.category_month_agg;
CREATE POLICY category_month_agg_scope ON core.category_month_agg FOR SELECT
USING (
    (SELECT ctx.role()) IN ('analyst', 'privacy_officer', 'contractor')
    OR ((SELECT ctx.role()) = 'regional_manager' AND region = (SELECT ctx.region()))
);
