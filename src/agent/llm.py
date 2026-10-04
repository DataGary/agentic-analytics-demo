"""Model access. Swap MODEL_NAME (any Ollama model with tool support) to compare models in evals."""

from __future__ import annotations

import json
import re
import uuid

from langchain_core.messages import AIMessage
from langchain_ollama import ChatOllama

from common.config import get_settings


def get_llm() -> ChatOllama:
    s = get_settings()
    return ChatOllama(
        model=s.model_name,
        base_url=s.ollama_url,
        temperature=s.model_temperature,
        reasoning=s.model_reasoning,
        num_ctx=s.model_num_ctx,
        keep_alive="30m",
    )


_JSON_BLOCK = re.compile(r"```(?:json|tool_code)?\s*(\{.*?\})\s*```", re.S)


def coerce_tool_calls(msg: AIMessage, tool_names: set[str]) -> AIMessage:
    """Fallback for small models: if the model wrote a tool call as JSON text instead of using the
    native tool-call channel, convert it into a real tool call."""
    if msg.tool_calls or not isinstance(msg.content, str):
        return msg
    candidates = _JSON_BLOCK.findall(msg.content) or ([msg.content.strip()] if msg.content.strip().startswith("{") else [])
    for raw in candidates:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = obj.get("name") or obj.get("tool")
        args = obj.get("arguments") or obj.get("args") or obj.get("parameters") or {}
        if name in tool_names and isinstance(args, dict):
            return AIMessage(
                content="",
                id=msg.id,
                additional_kwargs=msg.additional_kwargs,
                tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}", "type": "tool_call"}],
            )
    return msg
