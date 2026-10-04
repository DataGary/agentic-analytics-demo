-- Runs once, as the owner (POSTGRES_USER=app_owner), when the Postgres volume is first created.

CREATE SCHEMA IF NOT EXISTS analytics;

CREATE TABLE analytics.regions (
    id   int PRIMARY KEY,
    name text NOT NULL UNIQUE
);

CREATE TABLE analytics.stores (
    id        int PRIMARY KEY,
    name      text NOT NULL,
    city      text NOT NULL,
    region_id int  NOT NULL REFERENCES analytics.regions(id)
);

CREATE TABLE analytics.products (
    id         int PRIMARY KEY,
    name       text    NOT NULL,
    category   text    NOT NULL,
    unit_price numeric(10,2) NOT NULL
);

CREATE TABLE analytics.customers (
    id        int PRIMARY KEY,
    name      text NOT NULL,
    email     text NOT NULL,          -- PII: only exposed unmasked to admins via analytics.customers_v
    region_id int  NOT NULL REFERENCES analytics.regions(id)
);

-- One row per order line. region_id is denormalized from stores so the RLS policy is a cheap column check.
CREATE TABLE analytics.sales (
    id          bigserial PRIMARY KEY,
    order_id    bigint  NOT NULL,
    sale_date   date    NOT NULL,
    store_id    int     NOT NULL REFERENCES analytics.stores(id),
    region_id   int     NOT NULL REFERENCES analytics.regions(id),
    product_id  int     NOT NULL REFERENCES analytics.products(id),
    customer_id int     NOT NULL REFERENCES analytics.customers(id),
    qty         int     NOT NULL,
    revenue     numeric(12,2) NOT NULL
);

CREATE INDEX sales_date_idx   ON analytics.sales (sale_date);
CREATE INDEX sales_region_idx ON analytics.sales (region_id, sale_date);
CREATE INDEX sales_store_idx  ON analytics.sales (store_id);
