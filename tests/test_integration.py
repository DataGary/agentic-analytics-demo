"""Security tests against the running stack (docker compose up). Each layer is tested on its own,
bypassing the layers above it, to prove defense in depth actually holds."""

import httpx
import psycopg
import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from common.auth import mint_token, principal_for
from common.config import get_settings
from mcp_server import cube_client, db

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
async def fresh_pools():
    # pytest-asyncio gives each test its own event loop; pools/clients must not outlive it.
    db._reader = db._audit = None
    db._lock = __import__("asyncio").Lock()
    cube_client._client = None
    yield
    await db.close_pools()
    db._reader = db._audit = None


# ----------------------------------------------------------------------------- Postgres RLS (raw SQL path)

REGION_SQL = "SELECT DISTINCT region_id FROM sales ORDER BY 1"


async def test_rls_region_scoped():
    _, rows = await db.run_scoped(principal_for("carol"), REGION_SQL, 100)
    assert [r["region_id"] for r in rows] == [1]
    _, rows = await db.run_scoped(principal_for("dave"), REGION_SQL, 100)
    assert [r["region_id"] for r in rows] == [2]


async def test_rls_all_scope():
    _, rows = await db.run_scoped(principal_for("bob"), REGION_SQL, 100)
    assert [r["region_id"] for r in rows] == [1, 2, 3, 4]


def test_rls_fails_closed_without_identity():
    with psycopg.connect(get_settings().pg_reader_dsn) as conn:
        assert conn.execute("SELECT count(*) FROM analytics.sales").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM analytics.customers_v").fetchone()[0] == 0


def test_reader_cannot_read_raw_pii_or_write():
    with psycopg.connect(get_settings().pg_reader_dsn, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT email FROM analytics.customers")
        with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
            conn.execute("DELETE FROM analytics.sales")


async def test_email_masking_by_role():
    sql = "SELECT email FROM customers_v ORDER BY id LIMIT 1"
    _, admin = await db.run_scoped(principal_for("alice"), sql, 1)
    _, analyst = await db.run_scoped(principal_for("bob"), sql, 1)
    assert "***" not in admin[0]["email"] and "***@" in analyst[0]["email"]


# ----------------------------------------------------------------------------- Cube (semantic path), called directly

REVENUE_BY_REGION = {"measures": ["sales.revenue"], "dimensions": ["regions.name"]}


async def test_cube_region_filter_injected():
    rows = await cube_client.load(REVENUE_BY_REGION, mint_token(principal_for("carol")))
    assert [r["regions.name"] for r in rows] == ["East"]


async def test_cube_blocks_sales_viewer_detail_even_without_mcp():
    token = mint_token(principal_for("erin"))
    with pytest.raises(cube_client.CubeError, match="not permitted"):
        await cube_client.load({"measures": ["sales.revenue"], "dimensions": ["stores.name"]}, token)
    with pytest.raises(cube_client.CubeError, match="Granularity"):
        await cube_client.load({"measures": ["sales.revenue"],
                                "timeDimensions": [{"dimension": "sales.sale_date", "granularity": "day"}]}, token)
    assert len(await cube_client.load(REVENUE_BY_REGION, token)) == 4


async def test_cube_rejects_forged_token():
    import jwt
    forged = jwt.encode({"sub": "x", "role": "admin", "aud": "retail-analytics"}, "wrong-key-wrong-key-wrong-key-123")
    with pytest.raises(cube_client.CubeError):
        await cube_client.load(REVENUE_BY_REGION, forged)


# ----------------------------------------------------------------------------- MCP server over HTTP


def mcp_client(user: str | None):
    headers = {"Authorization": f"Bearer {mint_token(principal_for(user))}"} if user else {}
    return MultiServerMCPClient({"r": {"transport": "streamable_http", "url": get_settings().mcp_url, "headers": headers}})


async def test_mcp_requires_auth():
    r = httpx.post(get_settings().mcp_url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401


async def _call(user, tool, args):
    tools = {t.name: t for t in await mcp_client(user).get_tools()}
    return await tools[tool].ainvoke({"name": tool, "args": args, "id": "t", "type": "tool_call"})


async def test_mcp_run_sql_denied_for_regional_manager():
    msg = await _call("carol", "run_sql", {"sql": "SELECT count(*) FROM sales"})
    assert msg.status == "error" and "not permitted" in str(msg.content)


async def test_mcp_query_metrics_scoped():
    msg = await _call("dave", "query_metrics", {"measures": ["sales.revenue"], "dimensions": ["regions.name"]})
    assert msg.status != "error" and "West" in str(msg.content) and "East" not in str(msg.content)


async def test_mcp_sales_viewer_scope_error_is_audited():
    msg = await _call("erin", "query_metrics", {"measures": ["sales.revenue"], "dimensions": ["stores.name"]})
    assert msg.status == "error" and "not permitted for role sales_viewer" in str(msg.content)
    with psycopg.connect(get_settings().pg_admin_dsn) as conn:
        row = conn.execute("SELECT status, error FROM audit.tool_calls WHERE user_sub = 'erin' "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    assert row[0] == "denied" and "stores.name" in row[1]


async def test_mcp_run_sql_guard_feedback():
    msg = await _call("bob", "run_sql", {"sql": "DELETE FROM sales"})
    assert msg.status == "error" and "SQL rejected" in str(msg.content)
