-- 0001 core — the warehouse tables.
--
-- Three schemas, three trust levels:
--   core       base tables with raw PII. The agent role has NO access.
--   private    users, sessions, audit, approvals. The agent role has NO access.
--   analytics  masked, row-filtered views. The ONLY thing the agent role reads.
--
-- Base tables are owned by nl2sql_owner (a NOLOGIN, NOBYPASSRLS role),
-- never by the superuser that runs migrations: a superuser bypasses RLS
-- unconditionally, so anything it owned would leak through the views.

CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS private;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS ctx;

CREATE TABLE IF NOT EXISTS core.sales_reps (
    rep_id      text PRIMARY KEY,
    rep_name    text NOT NULL,             -- PII (employee)
    rep_email   text NOT NULL,             -- PII (employee)
    region      text NOT NULL
);

CREATE TABLE IF NOT EXISTS core.customers (
    customer_id      integer PRIMARY KEY,
    full_name        text NOT NULL,        -- PII
    email            text NOT NULL,        -- PII
    phone            text NOT NULL,        -- PII
    street_address   text NOT NULL,        -- PII
    city             text NOT NULL,        -- PII (quasi-identifier)
    postal_code      text NOT NULL,        -- PII (quasi-identifier)
    date_of_birth    date NOT NULL,        -- PII
    region           text NOT NULL,        -- CURRENT region (may differ from older orders)
    segment          text NOT NULL,
    assigned_rep_id  text NOT NULL REFERENCES core.sales_reps (rep_id),
    created_at       timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS core.products (
    product_id  integer PRIMARY KEY,
    name        text NOT NULL,             -- NOT unique: some names repeat across categories
    category    text NOT NULL,
    list_price  numeric(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS core.orders (
    order_id      integer PRIMARY KEY,
    customer_id   integer NOT NULL REFERENCES core.customers (customer_id),
    region        text NOT NULL,           -- region AT THE TIME of the order
    order_date    date NOT NULL,           -- the business date (use this for revenue)
    created_at    timestamptz NOT NULL,    -- when the record was keyed (0-4 days later)
    status        text NOT NULL CHECK (status IN ('completed', 'shipped', 'processing', 'cancelled')),
    total_amount  numeric(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS core.order_items (
    order_item_id  bigint PRIMARY KEY,
    order_id       integer NOT NULL REFERENCES core.orders (order_id),
    product_id     integer NOT NULL REFERENCES core.products (product_id),
    quantity       integer NOT NULL CHECK (quantity > 0),
    unit_price     numeric(10, 2) NOT NULL
);

-- Pre-aggregated, minimum-group-size summaries (refreshed by the service
-- role). These are what the contractor role may see instead of rows.
CREATE TABLE IF NOT EXISTS core.region_month_agg (
    region           text NOT NULL,
    month            date NOT NULL,
    orders           integer NOT NULL,
    customers        integer NOT NULL,
    revenue          numeric(14, 2) NOT NULL,
    avg_order_value  numeric(12, 2) NOT NULL,
    PRIMARY KEY (region, month)
);

CREATE TABLE IF NOT EXISTS core.category_month_agg (
    region       text NOT NULL,
    month        date NOT NULL,
    category     text NOT NULL,
    units        integer NOT NULL,
    revenue      numeric(14, 2) NOT NULL,
    order_lines  integer NOT NULL,
    PRIMARY KEY (region, month, category)
);

CREATE INDEX IF NOT EXISTS orders_region_date_idx ON core.orders (region, order_date);
CREATE INDEX IF NOT EXISTS orders_customer_idx ON core.orders (customer_id);
CREATE INDEX IF NOT EXISTS order_items_order_idx ON core.order_items (order_id);
CREATE INDEX IF NOT EXISTS customers_region_idx ON core.customers (region);
CREATE INDEX IF NOT EXISTS customers_rep_idx ON core.customers (assigned_rep_id);

ALTER TABLE core.sales_reps OWNER TO nl2sql_owner;
ALTER TABLE core.customers OWNER TO nl2sql_owner;
ALTER TABLE core.products OWNER TO nl2sql_owner;
ALTER TABLE core.orders OWNER TO nl2sql_owner;
ALTER TABLE core.order_items OWNER TO nl2sql_owner;
ALTER TABLE core.region_month_agg OWNER TO nl2sql_owner;
ALTER TABLE core.category_month_agg OWNER TO nl2sql_owner;
