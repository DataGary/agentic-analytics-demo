"""Every tool call goes through tool_call(): authenticate, authorize, trace, time, and audit."""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError
from opentelemetry import trace

from common.auth import Principal
from mcp_server import db
from mcp_server.auth import current_principal

log = logging.getLogger("mcp_server.audit")
tracer = trace.get_tracer("mcp_server")


@dataclass
class Call:
    principal: Principal
    tool: str
    args: dict[str, Any]
    query_text: str | None = None
    row_count: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


async def _write(call: Call, status: str, latency_ms: int, error: str | None, trace_id: str | None) -> None:
    try:
        async with (await db.audit_pool()).connection() as conn:
            await conn.execute(
                "INSERT INTO audit.tool_calls (trace_id, user_sub, role, tool, args, query_text, row_count, "
                "latency_ms, status, error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (trace_id, call.principal.sub, call.principal.role, call.tool, json.dumps(call.args, default=str),
                 call.query_text, call.row_count, latency_ms, status, error),
            )
    except Exception:  # auditing must never take the tool down, but it must be visible
        log.exception("audit write failed")


@asynccontextmanager
async def tool_call(tool: str, args: dict[str, Any]):
    try:
        p = current_principal()
    except Exception as e:
        raise ToolError("unauthenticated") from e

    call = Call(principal=p, tool=tool, args=args)
    extra = {"user": p.sub, "role": p.role, "tool": tool}
    with tracer.start_as_current_span(f"tool.{tool}") as span:
        span.set_attributes({"enduser.id": p.sub, "enduser.role": p.role, "tool.name": tool})
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else None
        t0 = time.perf_counter()

        if not p.policy.allows_tool(tool):
            msg = f"tool {tool} is not permitted for role {p.role}"
            await _write(call, "denied", 0, msg, trace_id)
            log.warning(msg, extra=extra)
            span.set_attribute("tool.status", "denied")
            raise ToolError(msg)

        try:
            yield call
        except PermissionError as e:  # ScopeError etc.
            await _write(call, "denied", int((time.perf_counter() - t0) * 1000), str(e), trace_id)
            log.warning("denied: %s", e, extra=extra)
            span.set_attribute("tool.status", "denied")
            raise ToolError(str(e)) from e
        except ToolError as e:
            await _write(call, "error", int((time.perf_counter() - t0) * 1000), str(e), trace_id)
            span.set_attribute("tool.status", "error")
            raise
        except Exception as e:
            await _write(call, "error", int((time.perf_counter() - t0) * 1000), str(e), trace_id)
            log.exception("tool failed", extra=extra)
            span.set_attribute("tool.status", "error")
            raise ToolError(f"{type(e).__name__}: {e}") from e
        else:
            latency = int((time.perf_counter() - t0) * 1000)
            span.set_attributes({"tool.status": "ok", "tool.row_count": call.row_count or 0})
            if call.query_text:
                span.set_attribute("tool.query", call.query_text)
            await _write(call, "ok", latency, None, trace_id)
            log.info("ok rows=%s latency_ms=%s", call.row_count, latency, extra=extra)
