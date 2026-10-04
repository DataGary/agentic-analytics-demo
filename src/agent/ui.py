"""Chainlit chat UI. Each tool call shows up as an expandable step so you can see what the agent did."""

from __future__ import annotations

import json
import uuid

import chainlit as cl

from agent.api import get_service
from common.auth import authenticate, demo_users, principal_for

EXAMPLES = {
    "admin": ["Revenue by region last month", "Forecast weekly revenue for the next 8 weeks",
              "Using SQL, list the 5 customers with the highest revenue in 2026 with their email"],
    "analyst": ["Top 5 product categories by units in 2025", "Using SQL, which store had the most orders on a single day?"],
    "regional_manager": ["Revenue by store last month", "Show West region revenue", "Forecast monthly units for the next 3 months"],
    "sales_viewer": ["Monthly revenue by region for 2025", "Revenue per store last month",
                     "List the last 20 sales with customer emails"],
}


@cl.password_auth_callback
def auth_callback(username: str, password: str) -> cl.User | None:
    p = authenticate(username, password)
    if p is None:
        return None
    return cl.User(identifier=p.sub, display_name=p.display, metadata={"role": p.role, "region_id": p.region_id})


@cl.on_chat_start
async def on_chat_start():
    user: cl.User = cl.user_session.get("user")
    p = principal_for(user.identifier)
    cl.user_session.set("thread_id", f"{p.sub}:{uuid.uuid4().hex[:12]}")
    others = ", ".join(f"`{u}` ({v['role']})" for u, v in demo_users().items() if u != p.sub)
    tools = ", ".join(f"`{t}`" for t in sorted(p.policy.tools))
    examples = "\n".join(f"- {e}" for e in EXAMPLES.get(p.role, []))
    await cl.Message(
        content=(
            f"Signed in as **{p.display}**: role `{p.role}`, row scope `{p.policy.row_scope}`"
            f"{f' (region {p.region_id})' if p.region_id else ''}.\n\n"
            f"Tools you can use: {tools}\n\nTry:\n{examples}\n\n"
            f"_To switch user, log out (top right) and sign in as {others}. Password for all: `demo`._"
        )
    ).send()


async def _ask_sql_approval(value: dict) -> dict:
    sql = "\n\n".join(c["sql"] for c in value.get("calls", []))
    res = await cl.AskActionMessage(
        content=f"The agent wants to run this SQL:\n```sql\n{sql}\n```",
        actions=[
            cl.Action(name="approve", payload={"action": "approve"}, label="✅ Run it"),
            cl.Action(name="edit", payload={"action": "edit"}, label="✏️ Edit"),
            cl.Action(name="reject", payload={"action": "reject"}, label="❌ Reject"),
        ],
        timeout=900,
    ).send()
    action = (res or {}).get("payload", {}).get("action", "reject")
    if action == "edit":
        edited = await cl.AskUserMessage(content="Send the corrected SQL:", timeout=900).send()
        return {"action": "edit", "sql": (edited or {}).get("output", "")} if edited else {"action": "reject"}
    return {"action": action, "reason": "rejected in UI"} if action == "reject" else {"action": action}


@cl.on_message
async def on_message(message: cl.Message):
    user: cl.User = cl.user_session.get("user")
    p = principal_for(user.identifier)
    thread_id = cl.user_session.get("thread_id")
    svc = get_service()

    text, resume = message.content, None
    while True:
        interrupt_value = None
        steps: dict[str, cl.Step] = {}
        try:
            async for ev in svc.stream(p, thread_id, message=text if resume is None else None, resume=resume):
                if ev["type"] == "thinking":
                    async with cl.Step(name="thinking", type="llm") as s:
                        s.output = ev["content"]
                elif ev["type"] == "tool_call":
                    step = cl.Step(name=ev["name"], type="tool", show_input="json", language="json")
                    step.input = ev["args"]
                    await step.send()
                    steps[ev["id"]] = step
                elif ev["type"] == "tool_result":
                    step = steps.get(ev["id"]) or cl.Step(name=ev["name"] or "tool", type="tool")
                    try:
                        step.output = json.dumps(json.loads(ev["content"]), indent=2)
                        step.language = "json"
                    except (json.JSONDecodeError, TypeError):
                        step.output = ev["content"]
                    step.is_error = ev["status"] == "error"
                    await step.update()
                elif ev["type"] == "interrupt":
                    interrupt_value = ev["value"]
                elif ev["type"] == "final":
                    await cl.Message(content=ev["content"]).send()
        except Exception as e:  # noqa: BLE001
            await cl.ErrorMessage(content=f"Something went wrong: {type(e).__name__}: {e}").send()
            return
        if interrupt_value is None:
            return
        resume = await _ask_sql_approval(interrupt_value)
