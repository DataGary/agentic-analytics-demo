"""Scope checks for semantic-layer queries. Mirrors cube/cube.js so a violation is rejected here
with a clear message before Cube is ever called (and Cube rejects it again if this is bypassed)."""

from __future__ import annotations

from typing import Any

from common.auth import Principal


class ScopeError(PermissionError):
    pass


def referenced_members(query: dict[str, Any]) -> set[str]:
    out: set[str] = set(query.get("measures", [])) | set(query.get("dimensions", []))
    out |= set(query.get("segments", []))
    out |= {td["dimension"] for td in query.get("timeDimensions", [])}

    def walk(filters):
        for f in filters or []:
            if "or" in f:
                walk(f["or"])
            elif "and" in f:
                walk(f["and"])
            else:
                out.add(f.get("member") or f.get("dimension"))

    walk(query.get("filters"))
    order = query.get("order") or {}
    out |= {o[0] for o in order} if isinstance(order, list) else set(order)
    return out


def check_query(p: Principal, query: dict[str, Any]) -> None:
    pol = p.policy
    if "*" not in pol.members:
        if query.get("ungrouped"):
            raise ScopeError(f"row-level (ungrouped) queries are not permitted for role {p.role}")
        if not query.get("measures"):
            raise ScopeError(f"role {p.role} may only run aggregated queries: include at least one measure")
        denied = sorted(m for m in referenced_members(query) if not pol.allows_member(m))
        if denied:
            allowed = ", ".join(sorted(pol.members))
            raise ScopeError(f"not permitted for role {p.role}: {', '.join(denied)}. Allowed members: {allowed}")
    for td in query.get("timeDimensions", []):
        if td.get("granularity") and not pol.allows_granularity(td["granularity"]):
            raise ScopeError(
                f"granularity {td['granularity']!r} is too fine for role {p.role}; use {pol.min_granularity} or coarser"
            )


def suppress_small_groups(p: Principal, rows: list[dict], orders_key: str = "sales.orders") -> tuple[list[dict], int]:
    """Drop groups with fewer than min_group_orders orders so aggregates can't single out individuals."""
    k = p.policy.min_group_orders
    if k <= 0:
        return rows, 0
    kept = [r for r in rows if float(r.get(orders_key) or 0) >= k]
    return kept, len(rows) - len(kept)
