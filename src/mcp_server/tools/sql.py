"""Text-to-SQL support: the schema the model may query, and guarded execution."""

SCHEMA_DOC = """Postgres schema `analytics` (read-only; rows are already filtered to what you may see).

regions(id int, name text)                       -- East, West, North, South
stores(id int, name text, city text, region_id int -> regions.id)
products(id int, name text, category text, unit_price numeric)
                                                 -- categories: Electronics, Home, Apparel, Grocery, Toys
customers_v(id int, name text, email text, region_id int -> regions.id)
                                                 -- email may be masked depending on your role
sales(id bigint, order_id bigint, sale_date date, store_id int -> stores.id,
      region_id int -> regions.id, product_id int -> products.id,
      customer_id int -> customers_v.id, qty int, revenue numeric)
                                                 -- one row per order line; data from 2024-10-01 to 2026-09-30

Rules: one SELECT statement only; only the tables above; results capped at 500 rows.
Postgres dialect: use date_trunc('month', sale_date), EXTRACT(YEAR FROM sale_date), ILIKE for text matching.
Example: SELECT p.category, SUM(s.revenue) AS revenue FROM sales s JOIN products p ON p.id = s.product_id
         WHERE s.sale_date >= DATE '2026-01-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 10
"""
