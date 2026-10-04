"""The agent as an explicit LangGraph state machine.

    START → guard_input ─(blocked)──────────────────────────────▶ END
                 │
                 ▼
               agent ──(no tool calls / step limit)──▶ guard_output ──▶ END
                 │  ▲
     (run_sql?)  │  └──────────────── tools ◀─(approve/edit)─┐
                 ├──────────────────────────▶ approve_sql ────┘ (human-in-the-loop interrupt)
                 └──(other tools)──────────▶ tools

Identity and tools live in the per-run Context (never in checkpointed state, never shown to the model).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt

from agent import guardrails
from agent.llm import coerce_tool_calls, get_llm
from agent.prompts import system_prompt
from common.auth import Principal
from common.config import get_settings

HISTORY_TURNS = 4  # past user turns kept in the prompt; a small model degrades with long context
OLD_TOOL_RESULT_CHARS = 800


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    sql_errors: int
    blocked: bool


@dataclass
class Context:
    principal: Principal
    tools: list[BaseTool] = field(default_factory=list)
    require_sql_approval: bool = True


# ----------------------------------------------------------------------------- helpers


def _trim_history(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Keep the last few user turns (cut at a HumanMessage so tool calls stay paired with results),
    and shorten tool results from earlier turns."""
    human_idx = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    start = human_idx[-HISTORY_TURNS] if len(human_idx) >= HISTORY_TURNS else 0
    kept = messages[start:]
    last_human = max((i for i, m in enumerate(kept) if isinstance(m, HumanMessage)), default=0)
    out = []
    for i, m in enumerate(kept):
        if i < last_human and isinstance(m, ToolMessage) and len(str(m.content)) > OLD_TOOL_RESULT_CHARS:
            m = m.model_copy(update={"content": str(m.content)[:OLD_TOOL_RESULT_CHARS] + " …(truncated)"})
        out.append(m)
    return out


# ----------------------------------------------------------------------------- nodes


async def guard_input(state: AgentState, runtime: Runtime[Context]) -> dict:
    fresh = {"steps": 0, "sql_errors": 0, "blocked": False}
    text = str(state["messages"][-1].content)
    reason = guardrails.check_input(text)
    if reason is None and get_settings().llm_input_guard:
        verdict = await get_llm().ainvoke(guardrails.LLM_GUARD_PROMPT.format(message=text))
        if "UNSAFE" in str(verdict.content).upper():
            reason = "I can't help with that request. I can only answer questions about sales data within your access."
    if reason:
        return {**fresh, "blocked": True, "messages": [AIMessage(content=reason)]}
    return fresh


async def agent(state: AgentState, runtime: Runtime[Context]) -> dict:
    ctx = runtime.context
    s = get_settings()
    tool_names = [t.name for t in ctx.tools]
    sys = SystemMessage(content=system_prompt(ctx.principal.role, tool_names))
    history = _trim_history(state["messages"])

    if state["steps"] >= s.max_agent_steps - 1:
        # Last step: no tools, force an answer with what we have.
        msg = await get_llm().ainvoke(
            [sys, *history, HumanMessage(content="Step limit reached. Answer now using only the tool results above.")]
        )
    else:
        msg = await get_llm().bind_tools(ctx.tools).ainvoke([sys, *history])
        msg = coerce_tool_calls(msg, set(tool_names))
    return {"messages": [msg], "steps": state["steps"] + 1}


def route_after_agent(state: AgentState, runtime: Runtime[Context]) -> Literal["approve_sql", "tools", "guard_output"]:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return "guard_output"
    if state["steps"] >= get_settings().max_agent_steps:
        return "guard_output"
    if runtime.context.require_sql_approval and any(tc["name"] == "run_sql" for tc in last.tool_calls):
        return "approve_sql"
    return "tools"


async def approve_sql(state: AgentState, runtime: Runtime[Context]) -> Command:
    """Pause the graph and ask the human to approve, edit or reject the generated SQL.
    The run is checkpointed; the API resumes it with Command(resume=decision)."""
    last: AIMessage = state["messages"][-1]
    sql_calls = [tc for tc in last.tool_calls if tc["name"] == "run_sql"]
    decision = interrupt({
        "type": "sql_approval",
        "calls": [{"id": tc["id"], "sql": tc["args"].get("sql", "")} for tc in sql_calls],
    })
    action = (decision or {}).get("action", "reject")

    if action == "approve":
        return Command(goto="tools")
    if action == "edit" and decision.get("sql"):
        calls = [
            {**tc, "args": {**tc["args"], "sql": decision["sql"]}} if tc["name"] == "run_sql" else tc
            for tc in last.tool_calls
        ]
        edited = AIMessage(content=last.content, id=last.id, tool_calls=calls, additional_kwargs=last.additional_kwargs)
        return Command(goto="tools", update={"messages": [edited]})
    reason = decision.get("reason") or "no reason given"
    declined = [
        ToolMessage(content=f"The user declined to run this query ({reason}). Do not retry it; ask how to proceed.",
                    tool_call_id=tc["id"], name=tc["name"], status="error")
        for tc in last.tool_calls
    ]
    return Command(goto="agent", update={"messages": declined})


async def tools(state: AgentState, runtime: Runtime[Context]) -> dict:
    last: AIMessage = state["messages"][-1]
    by_name = {t.name: t for t in runtime.context.tools}
    results: list[ToolMessage] = []
    sql_errors = state["sql_errors"]
    for tc in last.tool_calls:
        tool = by_name.get(tc["name"])
        if tool is None:  # model hallucinated a tool, or tried one its role doesn't have
            results.append(ToolMessage(content=f"Tool {tc['name']!r} is not available to you.",
                                       tool_call_id=tc["id"], name=tc["name"], status="error"))
            continue
        try:
            out = await tool.ainvoke({**tc, "type": "tool_call"})
        except Exception as e:  # transport failures etc.; MCP isError results are already ToolMessages
            out = ToolMessage(content=f"Tool error: {e}", tool_call_id=tc["id"], name=tc["name"], status="error")
        if tc["name"] == "run_sql" and out.status == "error":
            sql_errors += 1
            if sql_errors > get_settings().max_sql_repairs:
                out = out.model_copy(update={"content": f"{out.content}\nNo SQL attempts left. Do not call run_sql "
                                                        "again; explain the problem to the user."})
        results.append(out)
    return {"messages": results, "sql_errors": sql_errors}


async def guard_output(state: AgentState, runtime: Runtime[Context]) -> dict:
    last = state["messages"][-1]
    if not isinstance(last, AIMessage):
        return {}
    text = str(last.content or "").strip() or "I couldn't complete that request within the step limit."
    original = text
    if runtime.context.principal.policy.mask_pii:
        text = guardrails.redact_pii(text)
    bad = guardrails.ungrounded_numbers(text, guardrails.turn_tool_messages(state["messages"]))
    if bad:
        text += f"\n\n> ⚠️ These figures could not be verified against query results: {', '.join(bad[:5])}"
    if text != original or last.tool_calls:
        return {"messages": [AIMessage(content=text, id=last.id, additional_kwargs=last.additional_kwargs)]}
    return {}


# ----------------------------------------------------------------------------- graph


def build_graph() -> StateGraph:
    g = StateGraph(AgentState, context_schema=Context)
    g.add_node("guard_input", guard_input)
    g.add_node("agent", agent)
    g.add_node("approve_sql", approve_sql, destinations=("tools", "agent"))
    g.add_node("tools", tools)
    g.add_node("guard_output", guard_output)

    g.add_edge(START, "guard_input")
    g.add_conditional_edges("guard_input", lambda s: END if s["blocked"] else "agent", [END, "agent"])
    g.add_conditional_edges("agent", route_after_agent, ["approve_sql", "tools", "guard_output"])
    g.add_edge("tools", "agent")
    g.add_edge("guard_output", END)
    return g
