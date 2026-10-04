"""Authorization matrix: every user x tool, and the aggregate-only scope rules for sales_viewer."""

import pytest

from common.auth import principal_for
from mcp_server.policy import ScopeError, check_query, suppress_small_groups
from mcp_server.tools.semantic import Filter, build_query

ALL_TOOLS = ["describe_semantic_model", "query_metrics", "forecast", "get_sql_schema", "run_sql"]
EXPECTED_TOOLS = {
    "alice": set(ALL_TOOLS),
    "bob": set(ALL_TOOLS),
    "carol": {"describe_semantic_model", "query_metrics", "forecast"},
    "dave": {"describe_semantic_model", "query_metrics", "forecast"},
    "erin": {"describe_semantic_model", "query_metrics", "forecast"},
}


@pytest.mark.parametrize("user", EXPECTED_TOOLS)
@pytest.mark.parametrize("tool", ALL_TOOLS)
def test_tool_matrix(user, tool):
    assert principal_for(user).policy.allows_tool(tool) == (tool in EXPECTED_TOOLS[user])


def q(measures, dims=None, granularity=None, filters=None, order_by=None):
    return build_query(measures, dims, granularity, None, filters, order_by, True, 100)


ERIN_ALLOWED = [
    q(["sales.revenue"], ["regions.name"]),
    q(["revenue"], ["category"], "month"),
    q(["sales.units", "sales.orders"], None, "week"),
    q(["sales.avg_order_value"], ["regions.name"], "quarter", [Filter(member="products.category", values=["Toys"])]),
]
ERIN_DENIED = [
    (q([], ["regions.name"]), "at least one measure"),
    (q(["sales.revenue"], ["stores.name"]), "stores.name"),
    (q(["sales.revenue"], ["products.name"]), "products.name"),
    (q(["sales.revenue"], ["customers.email"]), "customers.email"),
    (q(["sales.revenue"], None, None, [Filter(member="customers.name", values=["x"])]), "customers.name"),
    (q(["sales.revenue"], None, None, None, "stores.city"), "stores.city"),
    (q(["sales.revenue"], ["sales.order_id"]), "sales.order_id"),
    (q(["sales.revenue"], None, "day"), "granularity"),
    ({**q(["sales.revenue"]), "ungrouped": True}, "ungrouped"),
]


@pytest.mark.parametrize("query", ERIN_ALLOWED)
def test_sales_viewer_allowed(query):
    check_query(principal_for("erin"), query)


@pytest.mark.parametrize("query, msg", ERIN_DENIED)
def test_sales_viewer_denied(query, msg):
    with pytest.raises(ScopeError, match=msg):
        check_query(principal_for("erin"), query)


@pytest.mark.parametrize("user", ["alice", "bob", "carol"])
@pytest.mark.parametrize("query, _", ERIN_DENIED[1:8])
def test_other_roles_not_scope_limited(user, query, _):
    check_query(principal_for(user), query)


def test_small_group_suppression():
    rows = [{"regions.name": "East", "sales.orders": 120}, {"regions.name": "Tiny", "sales.orders": 3}]
    kept, dropped = suppress_small_groups(principal_for("erin"), rows)
    assert dropped == 1 and kept == rows[:1]
    kept, dropped = suppress_small_groups(principal_for("alice"), rows)
    assert dropped == 0 and kept == rows


def test_date_range_without_granularity_is_a_single_total():
    query = build_query(["revenue"], None, None, "2025", None, None, True, 100)
    assert query["timeDimensions"] == [{"dimension": "sales.sale_date", "dateRange": ["2025-01-01", "2025-12-31"]}]
    check_query(principal_for("erin"), query)  # a total over a range is coarser than any bucket: allowed
