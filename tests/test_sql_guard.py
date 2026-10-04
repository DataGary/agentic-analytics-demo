import pytest

from mcp_server.sql_guard import MAX_ROWS, SqlRejected, check_sql


@pytest.mark.parametrize("sql", [
    "SELECT region_id, SUM(revenue) FROM sales GROUP BY 1",
    "SELECT s.name, SUM(x.revenue) FROM sales x JOIN stores s ON s.id = x.store_id GROUP BY 1",
    "WITH m AS (SELECT date_trunc('month', sale_date) AS m, SUM(revenue) r FROM sales GROUP BY 1) SELECT * FROM m",
    "SELECT * FROM analytics.products",
    "SELECT name, email FROM customers_v LIMIT 5",
    "SELECT category FROM products UNION SELECT name FROM regions",
])
def test_allows_reads(sql):
    assert check_sql(sql).sql


@pytest.mark.parametrize("sql, reason", [
    ("DELETE FROM sales", "only SELECT"),
    ("UPDATE sales SET revenue = 0", "only SELECT"),
    ("DROP TABLE sales", "only SELECT"),
    ("SELECT 1 FROM sales; DROP TABLE sales", "exactly one statement"),
    ("WITH d AS (DELETE FROM sales RETURNING *) SELECT * FROM d", "DELETE"),
    ("SELECT pg_sleep(30) FROM sales", "pg_sleep"),
    ("SELECT set_config('app.row_scope', 'all', true) FROM sales", "set_config"),
    ("SELECT current_setting('app.region_id') FROM sales", "current_setting"),
    ("SELECT * FROM pg_catalog.pg_roles", "not allowed"),
    ("SELECT * FROM information_schema.tables", "not allowed"),
    ("SELECT email FROM customers", "customers_v"),
    ("SELECT * INTO stolen FROM sales", "INTO"),
    ("SELECT * FROM sales FOR UPDATE", "LOCK"),
    ("SELECT 1", "at least one allowed table"),
    ("SELEC * FROM sales", None),
])
def test_rejects(sql, reason):
    with pytest.raises(SqlRejected, match=reason):
        check_sql(sql)


def test_limit_injected_and_capped():
    assert check_sql("SELECT * FROM sales").sql.endswith(f"LIMIT {MAX_ROWS}")
    assert check_sql("SELECT * FROM sales LIMIT 100000").sql.endswith(f"LIMIT {MAX_ROWS}")
    r = check_sql("SELECT * FROM sales LIMIT 7")
    assert r.sql.endswith("LIMIT 7") and not r.limit_applied
