"""AgentService: one place that runs a turn for a user and yields UI-friendly events.
Used by the HTTP API, the Chainlit UI and the eval runner, so all three behave identically."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.types import Command

from agent.graph import Context, build_graph
from agent.guardrails import redact_pii
from common.auth import Principal, mint_token
from common.config import get_settings

try:
    from openinference.instrumentation import using_attributes
except ImportError:  # pragma: no cover
    from contextlib import nullcontext as using_attributes  # type: ignore[assignment]


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return str(content)


class AgentService:
    def __init__(self, checkpointer=None):
        self.graph = build_graph().compile(checkpointer=checkpointer)

    async def tools_for(self, p: Principal) -> list[BaseTool]:
        """Load MCP tools with this user's token, and bind only the ones their role may use.
        (The MCP server re-checks every call; this just keeps forbidden tools out of the model's view.)"""
        client = MultiServerMCPClient({
            "retail": {
                "transport": "streamable_http",
                "url": get_settings().mcp_url,
                "headers": {"Authorization": f"Bearer {mint_token(p)}"},
            }
        })
        return [t for t in await client.get_tools() if p.policy.allows_tool(t.name)]

    @staticmethod
    def check_thread(p: Principal, thread_id: str) -> None:
        if not thread_id.startswith(f"{p.sub}:"):
            raise PermissionError("thread does not belong to this user")

    async def stream(
        self,
        p: Principal,
        thread_id: str,
        message: str | None = None,
        resume: dict | None = None,
        require_sql_approval: bool | None = None,
    ) -> AsyncIterator[dict]:
        """Yield events: thinking, tool_call, tool_result, interrupt, final, error."""
        self.check_thread(p, thread_id)
        if require_sql_approval is None:
            require_sql_approval = get_settings().require_sql_approval
        ctx = Context(principal=p, tools=await self.tools_for(p), require_sql_approval=require_sql_approval)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}
        graph_input: Any = Command(resume=resume) if resume is not None else {"messages": [HumanMessage(content=message or "")]}
        scrub = redact_pii if p.policy.mask_pii else (lambda s: s)

        with using_attributes(session_id=thread_id, user_id=p.sub, metadata={"role": p.role}):
            async for update in self.graph.astream(graph_input, config, context=ctx, stream_mode="updates"):
                for node, delta in update.items():
                    if node == "__interrupt__":
                        for intr in delta:
                            yield {"type": "interrupt", "value": intr.value}
                        continue
                    for m in (delta or {}).get("messages", []) if isinstance(delta, dict) else []:
                        if node == "agent" and isinstance(m, AIMessage):
                            if thinking := m.additional_kwargs.get("reasoning_content"):
                                yield {"type": "thinking", "content": scrub(thinking)}
                            for tc in m.tool_calls:
                                yield {"type": "tool_call", "id": tc["id"], "name": tc["name"], "args": tc["args"]}
                        elif isinstance(m, ToolMessage):
                            yield {"type": "tool_result", "id": m.tool_call_id, "name": m.name,
                                   "status": m.status, "content": scrub(_text(m.content))[:4000]}

        state = await self.graph.aget_state(config)
        if state.next:  # paused on an interrupt; the caller resumes with resume=...
            return
        last = state.values["messages"][-1]
        yield {"type": "final", "content": _text(last.content)}

    async def run(self, p: Principal, thread_id: str, message: str, auto_approve: bool = True) -> dict:
        """Non-streaming helper (evals/tests): runs to completion, auto-approving SQL if asked."""
        events: list[dict] = []
        resume = None
        for _ in range(5):
            interrupted = False
            async for ev in self.stream(p, thread_id, message if resume is None else None, resume):
                events.append(ev)
                if ev["type"] == "interrupt":
                    interrupted = True
            if not interrupted:
                break
            resume = {"action": "approve" if auto_approve else "reject", "reason": "auto"}
        final = next((e["content"] for e in reversed(events) if e["type"] == "final"), "")
        return {"answer": final, "events": events,
                "tools_called": [e["name"] for e in events if e["type"] == "tool_call"]}


def sse(event: dict) -> str:
    return json.dumps(event, default=str)
