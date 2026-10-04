"""Semantic-layer tools: discover metrics, and query them through Cube.

This is the preferred path for the agent: the model picks named metrics and dimensions instead of
writing SQL, which is far more reliable for a small model and keeps business logic in one place.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from common.auth import Principal
from mcp_server import cube_client
from mcp_server.policy import check_query, suppress_small_groups

Granularity = Literal["day", "week", "month", "quarter", "year"]

TIME_DIMENSION = "sales.sale_date"

# Friendly aliases a small model tends to use instead of fully qualified member names.
ALIASES = {
    "revenue": "sales.revenue", "sales": "sales.revenue", "units": "sales.units", "orders": "sales.orders",
    "aov": "sales.avg_order_value", "avg_order_value": "sales.avg_order_value",
    "region": "regions.name", "regions": "regions.name", "store": "stores.name", "stores": "stores.name",
    "city": "stores.city", "product": "products.name", "products": "products.name",
    "category": "products.category", "customer": "customers.name", "date": TIME_DIMENSION,
}


class Filter(BaseModel):
    member: str = Field(description="Member to filter on, e.g. 'regions.name' or 'products.category'")
    operator: Literal["equals", "notEquals", "contains", "notContains", "gt", "gte", "lt", "lte", "set", "notSet"] = "equals"
    values: list[str] = Field(default_factory=list, description="Values as strings, e.g. ['East']")


def resolve_member(name: str) -> str:
    n = name.strip()
    if "." in n:
        return n
    return ALIASES.get(n.lower(), n)


def normalize_date_range(date_range: str | None) -> str | list[str] | None:
    if not date_range:
        return None
    s = date_range.strip()
    if re.fullmatch(r"\d{4}", s):
        return [f"{s}-01-01", f"{s}-12-31"]
    if m := re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:to|-|–|\.\.)\s*(\d{4}-\d{2}-\d{2})", s):
        return [m.group(1), m.group(2)]
    return s  # relative ranges like "last month", "this year", "last 6 months" are understood by Cube


def build_query(
    measures: list[str],
    dimensions: list[str] | None,
    granularity: str | None,
    date_range: str | None,
    filters: list[Filter] | None,
    order_by: str | None,
    descending: bool,
    limit: int,
) -> dict[str, Any]:
    q: dict[str, Any] = {
        "measures": [resolve_member(m) for m in measures],
        "dimensions": [resolve_member(d) for d in dimensions or [] if resolve_member(d) != TIME_DIMENSION],
        "filters": [
            {"member": resolve_member(f.member), "operator": f.operator, "values": [str(v) for v in f.values]}
            for f in filters or []
        ],
        "limit": max(1, min(limit, 5000)),
    }
    time_in_dims = TIME_DIMENSION in [resolve_member(d) for d in dimensions or []]
    if granularity or date_range or time_in_dims:
        td: dict[str, Any] = {"dimension": TIME_DIMENSION}
        # A date range without a granularity means "one total for the whole range". Don't bucket it:
        # a small model can't reliably add up 12 monthly rows to answer "total revenue in 2025".
        if granularity or time_in_dims:
            td["granularity"] = granularity or "month"
        if dr := normalize_date_range(date_range):
            td["dateRange"] = dr
        q["timeDimensions"] = [td]
    if order_by:
        q["order"] = {resolve_member(order_by): "desc" if descending else "asc"}
    elif q.get("timeDimensions", [{}])[0].get("granularity"):
        q["order"] = {TIME_DIMENSION: "asc"}
    return q


def _tidy(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        t = {}
        for k, v in r.items():
            if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T00:00:00\.000", v):
                v = v[:10]
            elif isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v):
                v = round(float(v), 2)
            t[k] = v
        out.append(t)
    return out


async def run_cube_query(p: Principal, token: str, query: dict[str, Any]) -> tuple[list[dict], int]:
    """Policy check -> Cube (with the caller's JWT) -> small-group suppression."""
    check_query(p, query)
    added_orders = False
    if p.policy.min_group_orders > 0 and "sales.orders" not in query["measures"]:
        query = {**query, "measures": [*query["measures"], "sales.orders"]}
        added_orders = True
    rows = _tidy(await cube_client.load(query, token))
    rows, suppressed = suppress_small_groups(p, rows)
    if added_orders:
        rows = [{k: v for k, v in r.items() if k != "sales.orders"} for r in rows]
    return rows, suppressed


async def describe_model(p: Principal, token: str) -> str:
    meta = await cube_client.meta(token)
    measures, dims, times = [], [], []
    for cube in meta.get("cubes", []):
        for m in cube.get("measures", []):
            if p.policy.allows_member(m["name"]) and m.get("isVisible", True):
                measures.append(f"- {m['name']}: {m.get('description') or m.get('shortTitle', '')}")
        for d in cube.get("dimensions", []):
            if d["name"].endswith(".id") or not p.policy.allows_member(d["name"]) or not d.get("isVisible", True):
                continue
            line = f"- {d['name']}: {d.get('description') or d.get('shortTitle', '')}"
            (times if d.get("type") == "time" else dims).append(line)
    gran = "day|week|month|quarter|year"
    if p.policy.min_granularity:
        from common.auth import GRANULARITY_ORDER
        gran = "|".join(GRANULARITY_ORDER[GRANULARITY_ORDER.index(p.policy.min_granularity):])
    notes = []
    if p.policy.row_scope == "region":
        notes.append("Results are automatically limited to your region.")
    if p.policy.min_group_orders:
        notes.append(f"Groups with fewer than {p.policy.min_group_orders} orders are suppressed.")
    return "\n".join(
        ["MEASURES:", *measures, "DIMENSIONS:", *dims, f"TIME (granularity: {gran}):", *times]
        + (["NOTES:", *notes] if notes else [])
    )
