"""MCP server exposing the analytics tools over Streamable HTTP, protected by bearer JWTs.

Shows all three MCP primitives: tools (actions), a resource (readable context) and a prompt (template).
Run: uv run python -m mcp_server.server
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

import psycopg
import uvicorn
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from common.logging import setup_logging
from common.telemetry import setup_tracing
from mcp_server import cube_client, db
from mcp_server.audit import tool_call
from mcp_server.auth import JwtVerifier, current_principal, current_token
from mcp_server.sql_guard import SqlRejected, check_sql
from mcp_server.tools.forecast import forecast_series
from mcp_server.tools.semantic import Filter, Granularity, build_query, describe_model, run_cube_query
from mcp_server.tools.sql import SCHEMA_DOC

log = logging.getLogger("mcp_server")


mcp = FastMCP(
    "retail-analytics",
    instructions="Retail sales analytics. Prefer query_metrics; use run_sql only when metrics can't answer.",
    token_verifier=JwtVerifier(),
    auth=AuthSettings(
        issuer_url="http://agent:8000",  # the agent service issues tokens in this demo; an IdP in production
        resource_server_url="http://mcp-server:8001/mcp",
        validate_token_resource=False,  # JwtVerifier checks the audience itself
    ),
    host="0.0.0.0",
    port=8001,
    stateless_http=True,  # no sticky sessions: any replica can serve any request
    json_response=True,
)


# ----------------------------------------------------------------------------- tools


@mcp.tool()
async def describe_semantic_model() -> str:
    """List the metrics (measures), dimensions and time granularities you are allowed to query.
    Call this first if you are unsure which member names exist."""
    async with tool_call("describe_semantic_model", {}) as call:
        text = await describe_model(call.principal, current_token())
        call.row_count = text.count("\n- ")
        return text


@mcp.tool()
async def query_metrics(
    measures: Annotated[list[str], Field(description="Metrics to aggregate, e.g. ['sales.revenue']")],
    dimensions: Annotated[list[str] | None, Field(description="Group-by members, e.g. ['regions.name']")] = None,
    granularity: Annotated[Granularity | None, Field(description="Time bucket for sales.sale_date")] = None,
    date_range: Annotated[
        str | None, Field(description="'last month', 'this year', 'last 6 months', '2025', or '2025-01-01 to 2025-03-31'")
    ] = None,
    filters: Annotated[list[Filter] | None, Field(description="Optional filters")] = None,
    order_by: Annotated[str | None, Field(description="Member to sort by, e.g. 'sales.revenue'")] = None,
    descending: bool = True,
    limit: Annotated[int, Field(ge=1, le=1000)] = 100,
) -> dict:
    """Query aggregated business metrics from the semantic layer (preferred over SQL).
    Example: measures=['sales.revenue'], dimensions=['regions.name'], date_range='last month'."""
    args = {k: v for k, v in locals().items() if v is not None}
    args["filters"] = [f.model_dump() for f in filters or []]
    async with tool_call("query_metrics", args) as call:
        q = build_query(measures, dimensions, granularity, date_range, filters, order_by, descending, limit)
        call.query_text = json.dumps(q)
        rows, suppressed = await run_cube_query(call.principal, current_token(), q)
        call.row_count = len(rows)
        out = {"row_count": len(rows), "rows": rows}
        if suppressed:
            out["note"] = f"{suppressed} small group(s) suppressed for privacy"
        return out


@mcp.tool()
async def forecast(
    measure: Annotated[str, Field(description="Metric to forecast, e.g. 'sales.revenue' or 'sales.units'")] = "sales.revenue",
    granularity: Annotated[Granularity, Field(description="day, week or month")] = "week",
    horizon: Annotated[int, Field(ge=1, le=52, description="How many periods ahead")] = 8,
    filters: Annotated[list[Filter] | None, Field(description="Optional filters, e.g. a region or category")] = None,
) -> dict:
    """Forecast a metric into the future with Holt-Winters exponential smoothing.
    Returns point forecasts with 80% intervals and a backtest error (MAPE)."""
    args = {"measure": measure, "granularity": granularity, "horizon": horizon,
            "filters": [f.model_dump() for f in filters or []]}
    async with tool_call("forecast", args) as call:
        if granularity not in ("day", "week", "month"):
            raise ToolError("forecast supports day, week or month granularity")
        q = build_query([measure], None, granularity, None, filters, None, False, 5000)
        call.query_text = json.dumps(q)
        rows, _ = await run_cube_query(call.principal, current_token(), q)
        key = q["measures"][0]
        tkey = f"sales.sale_date.{granularity}"
        try:
            result = forecast_series([r[tkey] for r in rows], [float(r.get(key) or 0) for r in rows], granularity, horizon)
        except ValueError as e:
            raise ToolError(str(e)) from e
        call.row_count = len(result["forecast"])
        return {"measure": key, **result}


@mcp.tool()
async def get_sql_schema() -> str:
    """Return the database tables and columns available for run_sql. Call before writing SQL."""
    async with tool_call("get_sql_schema", {}) as call:
        call.row_count = 5
        return SCHEMA_DOC


@mcp.tool()
async def run_sql(
    sql: Annotated[str, Field(description="A single Postgres SELECT statement over the allowed tables")],
    max_rows: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict:
    """Run a read-only SQL SELECT against the sales database. Use only when query_metrics can't answer
    (e.g. row-level detail, custom calculations). Rows are automatically limited to what you may see."""
    async with tool_call("run_sql", {"sql": sql, "max_rows": max_rows}) as call:
        try:
            guarded = check_sql(sql)
        except SqlRejected as e:
            call.query_text = sql
            raise ToolError(f"SQL rejected: {e}") from e
        call.query_text = guarded.sql
        try:
            columns, rows = await db.run_scoped(call.principal, guarded.sql, max_rows)
        except psycopg.Error as e:
            msg = e.diag.message_primary if getattr(e, "diag", None) else str(e)
            raise ToolError(f"Postgres error: {msg}. Fix the SQL and try again.") from e
        call.row_count = len(rows)
        return {"sql_executed": guarded.sql, "columns": columns, "row_count": len(rows), "rows": rows,
                "truncated": len(rows) >= max_rows}


# ----------------------------------------------------------------------------- resource + prompt


@mcp.resource("semantic://model", mime_type="text/plain")
async def semantic_model() -> str:
    """The semantic model (metrics and dimensions) visible to the caller."""
    return await describe_model(current_principal(), current_token())


@mcp.prompt()
def analyst_system_prompt(role: str = "analyst") -> str:
    """System prompt for an analytics assistant working with these tools."""
    from agent.prompts import system_prompt

    return system_prompt(role=role, tools=["describe_semantic_model", "query_metrics", "forecast", "run_sql"])


# ----------------------------------------------------------------------------- health


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    checks = {}
    for name, fn in (("postgres", db.ping), ("cube", cube_client.ping)):
        try:
            checks[name] = bool(await fn())
        except Exception as e:  # noqa: BLE001
            checks[name] = f"error: {e}"
    ok = all(v is True for v in checks.values())
    return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)


def main() -> None:
    setup_logging()
    setup_tracing("mcp-server")
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=8001, log_config=None)


if __name__ == "__main__":
    main()
