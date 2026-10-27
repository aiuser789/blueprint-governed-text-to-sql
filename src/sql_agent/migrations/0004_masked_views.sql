-- 0004 masked views — the only surface the agent role can read.
--
-- Masking happens HERE, inside the database, before a single value
-- reaches the application or the model. The tier comes from ctx.pii_tier()
-- (the session), never from the question.
--
--   column           full                 partial               masked
--   full_name        Jane Doe             Jane D.               Customer #4821
--   email            jane.doe@example.com j***@example.com      [REDACTED]
--   phone            +1-555-0182          ***-***-0182          [REDACTED]
--   street_address   12 Oak St            [REDACTED]            [REDACTED]
--   city             Charlotte            Charlotte             [REDACTED]
--   postal_code      28202                282**                 [REDACTED]
--   date_of_birth    1984-03-09           1984                  [REDACTED]
--
-- Because the masked value is computed in the view's output, a filter
-- such as WHERE full_name LIKE 'J%' runs against "Customer #4821", not
-- against the real name — it cannot be used to probe raw values.
-- security_barrier stops the planner from pushing a caller's leaky
-- functions underneath the view's own row filter.
--
-- The views are owned by nl2sql_owner (not security_invoker), so the
-- agent role needs no privilege on core.* at all; FORCE RLS (0003)
-- keeps the owner subject to the row policies.

CREATE OR REPLACE VIEW analytics.customers WITH (security_barrier = true) AS
SELECT
    c.customer_id,
    c.region,
    c.segment,
    c.assigned_rep_id,
    c.created_at,
    CASE
        WHEN date_part('year', age(DATE '2026-09-30', c.date_of_birth)) < 30 THEN 'under 30'
        WHEN date_part('year', age(DATE '2026-09-30', c.date_of_birth)) < 45 THEN '30-44'
        WHEN date_part('year', age(DATE '2026-09-30', c.date_of_birth)) < 60 THEN '45-59'
        ELSE '60+'
    END AS age_band,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN c.full_name
        WHEN 'partial' THEN split_part(c.full_name, ' ', 1) || ' ' || left(split_part(c.full_name, ' ', 2), 1) || '.'
        ELSE 'Customer #' || c.customer_id
    END AS full_name,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN c.email
        WHEN 'partial' THEN left(c.email, 1) || '***@' || split_part(c.email, '@', 2)
        ELSE '[REDACTED]'
    END AS email,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN c.phone
        WHEN 'partial' THEN '***-***-' || right(c.phone, 4)
        ELSE '[REDACTED]'
    END AS phone,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN c.street_address
        ELSE '[REDACTED]'
    END AS street_address,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN c.city
        WHEN 'partial' THEN c.city
        ELSE '[REDACTED]'
    END AS city,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN c.postal_code
        WHEN 'partial' THEN left(c.postal_code, 3) || '**'
        ELSE '[REDACTED]'
    END AS postal_code,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN to_char(c.date_of_birth, 'YYYY-MM-DD')
        WHEN 'partial' THEN to_char(c.date_of_birth, 'YYYY')
        ELSE '[REDACTED]'
    END AS date_of_birth
FROM core.customers c;

CREATE OR REPLACE VIEW analytics.sales_reps WITH (security_barrier = true) AS
SELECT
    r.rep_id,
    r.region,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN r.rep_name
        WHEN 'partial' THEN split_part(r.rep_name, ' ', 1) || ' ' || left(split_part(r.rep_name, ' ', 2), 1) || '.'
        ELSE 'Rep ' || r.rep_id
    END AS rep_name,
    CASE (SELECT ctx.pii_tier())
        WHEN 'full' THEN r.rep_email
        ELSE '[REDACTED]'
    END AS rep_email
FROM core.sales_reps r;

CREATE OR REPLACE VIEW analytics.orders WITH (security_barrier = true) AS
SELECT order_id, customer_id, region, order_date, created_at, status, total_amount
FROM core.orders;

CREATE OR REPLACE VIEW analytics.order_items WITH (security_barrier = true) AS
SELECT order_item_id, order_id, product_id, quantity, unit_price
FROM core.order_items;

CREATE OR REPLACE VIEW analytics.products WITH (security_barrier = true) AS
SELECT product_id, name, category, list_price
FROM core.products;

-- Minimum group size 10: a group small enough to point at a handful of
-- customers is never published, whatever the caller's role.
CREATE OR REPLACE VIEW analytics.region_month_summary WITH (security_barrier = true) AS
SELECT region, month, orders, customers, revenue, avg_order_value
FROM core.region_month_agg
WHERE orders >= 10 AND customers >= 10;

CREATE OR REPLACE VIEW analytics.category_month_summary WITH (security_barrier = true) AS
SELECT region, month, category, units, revenue, order_lines
FROM core.category_month_agg
WHERE order_lines >= 10;

ALTER VIEW analytics.customers OWNER TO nl2sql_owner;
ALTER VIEW analytics.sales_reps OWNER TO nl2sql_owner;
ALTER VIEW analytics.orders OWNER TO nl2sql_owner;
ALTER VIEW analytics.order_items OWNER TO nl2sql_owner;
ALTER VIEW analytics.products OWNER TO nl2sql_owner;
ALTER VIEW analytics.region_month_summary OWNER TO nl2sql_owner;
ALTER VIEW analytics.category_month_summary OWNER TO nl2sql_owner;

-- Refresh of the minimum-group-size aggregates. SECURITY DEFINER under
-- the migrating (service) role, executable by the service role only.
CREATE OR REPLACE FUNCTION private.refresh_aggregates() RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = core, pg_temp
AS $$
BEGIN
    DELETE FROM core.region_month_agg;
    INSERT INTO core.region_month_agg (region, month, orders, customers, revenue, avg_order_value)
    SELECT region,
           date_trunc('month', order_date)::date,
           count(*),
           count(DISTINCT customer_id),
           round(sum(total_amount), 2),
           round(avg(total_amount), 2)
    FROM core.orders
    WHERE status <> 'cancelled'
    GROUP BY 1, 2
    HAVING count(*) >= 10;

    DELETE FROM core.category_month_agg;
    INSERT INTO core.category_month_agg (region, month, category, units, revenue, order_lines)
    SELECT o.region,
           date_trunc('month', o.order_date)::date,
           p.category,
           sum(i.quantity),
           round(sum(i.quantity * i.unit_price), 2),
           count(*)
    FROM core.order_items i
    JOIN core.orders o ON o.order_id = i.order_id
    JOIN core.products p ON p.product_id = i.product_id
    WHERE o.status <> 'cancelled'
    GROUP BY 1, 2, 3
    HAVING count(*) >= 10;
END
$$;
