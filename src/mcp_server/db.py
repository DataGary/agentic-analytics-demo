"""Postgres access for the MCP server: a read-only RLS-bound pool and an audit-writer pool."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from common.auth import Principal
from common.config import get_settings

_reader: AsyncConnectionPool | None = None
_audit: AsyncConnectionPool | None = None
_lock = asyncio.Lock()


async def open_pools() -> None:
    """Idempotent; called lazily on first use so the pools live for the whole process."""
    global _reader, _audit
    async with _lock:
        if _reader is not None:
            return
        await _open()


async def _open() -> None:
    global _reader, _audit
    s = get_settings()
    _reader = AsyncConnectionPool(s.pg_reader_dsn, min_size=1, max_size=10, open=False)
    _audit = AsyncConnectionPool(s.pg_audit_dsn, min_size=1, max_size=4, open=False)
    await _reader.open()
    await _audit.open()


async def close_pools() -> None:
    for pool in (_reader, _audit):
        if pool is not None:
            await pool.close()


async def audit_pool() -> AsyncConnectionPool:
    await open_pools()
    return _audit  # type: ignore[return-value]


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


async def run_scoped(p: Principal, sql: str, max_rows: int) -> tuple[list[str], list[dict]]:
    """Run an already-guarded SELECT as mcp_reader with the caller's identity bound to the transaction.

    SET LOCAL / set_config(..., true) only lasts for this transaction, so a pooled connection
    can never leak one user's scope into another user's query.
    """
    await open_pools()
    async with _reader.connection() as conn:  # type: ignore[union-attr]
        async with conn.transaction():
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute(
                "SELECT set_config('app.row_scope', %s, true), set_config('app.region_id', %s, true), "
                "set_config('app.mask_pii', %s, true)",
                (p.policy.row_scope, str(p.region_id or ""), "true" if p.policy.mask_pii else "false"),
            )
            await conn.execute("SET LOCAL statement_timeout = '5s'")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                columns = [d.name for d in cur.description or []]
                rows = await cur.fetchmany(max_rows)
    return columns, [{k: _jsonable(v) for k, v in r.items()} for r in rows]


async def ping() -> bool:
    await open_pools()
    async with _reader.connection() as conn:  # type: ignore[union-attr]
        await conn.execute("SELECT 1")
    return True
