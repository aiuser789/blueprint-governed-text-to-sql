"""The retail data's tables in ``core.*`` and the summaries built from them.

The tables themselves are created by the numbered SQL migrations
(``src/sql_agent/migrations/0001_core.sql`` onwards) — they stay there so a
database that is already migrated keeps its migration history.
"""

# Every core.* table of the retail data: FORCE ROW LEVEL SECURITY is lifted
# on these (inside one transaction) only while the retail data is loaded or summarised.
TABLES = ("sales_reps", "products", "customers", "orders", "order_items", "region_month_agg", "category_month_agg")

# Rebuilds the minimum-group-size monthly summaries the contractor role reads.
REFRESH_AGGREGATES_SQL = """
DELETE FROM core.region_month_agg;
INSERT INTO core.region_month_agg (region, month, orders, customers, revenue, avg_order_value)
SELECT region, date_trunc('month', order_date)::date, count(*), count(DISTINCT customer_id),
       round(sum(total_amount), 2), round(avg(total_amount), 2)
FROM core.orders WHERE status <> 'cancelled'
GROUP BY 1, 2 HAVING count(*) >= 10;
DELETE FROM core.category_month_agg;
INSERT INTO core.category_month_agg (region, month, category, units, revenue, order_lines)
SELECT o.region, date_trunc('month', o.order_date)::date, p.category, sum(i.quantity),
       round(sum(i.quantity * i.unit_price), 2), count(*)
FROM core.order_items i JOIN core.orders o ON o.order_id = i.order_id JOIN core.products p ON p.product_id = i.product_id
WHERE o.status <> 'cancelled'
GROUP BY 1, 2, 3 HAVING count(*) >= 10;
"""
