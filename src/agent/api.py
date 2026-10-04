"""Agent HTTP service: login, streaming chat (SSE), human-in-the-loop resume, health, and the Chainlit UI.

Run: uv run uvicorn agent.api:app --port 8000
"""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from agent.service import AgentService, sse
from common.auth import Principal, authenticate, mint_token, verify_token
from common.config import get_settings
from common.logging import setup_logging
from common.telemetry import setup_tracing

log = logging.getLogger("agent.api")
_service: AgentService | None = None


def get_service() -> AgentService:
    if _service is None:
        raise RuntimeError("service not started")
    return _service


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _service
    setup_logging()
    setup_tracing("agent")
    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor

        LangChainInstrumentor().instrument()  # LLM calls, tool calls and graph nodes become spans
    except ImportError:  # pragma: no cover
        log.warning("openinference not installed; LLM spans disabled")

    # Conversation memory: LangGraph checkpoints every step to Postgres, so a thread survives restarts
    # and a human-in-the-loop pause can be resumed by any replica.
    async with AsyncPostgresSaver.from_conn_string(get_settings().pg_checkpoint_dsn) as saver:
        await saver.setup()
        _service = AgentService(checkpointer=saver)
        log.info("agent ready (model=%s)", get_settings().model_name)
        yield
    _service = None


app = FastAPI(title="Retail analytics agent", lifespan=lifespan)
bearer = HTTPBearer()


def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> Principal:
    try:
        return verify_token(creds.credentials)
    except (jwt.InvalidTokenError, PermissionError) as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


class LoginRequest(BaseModel):
    username: str
    password: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    thread_id: str | None = None


class ResumeRequest(BaseModel):
    thread_id: str
    decision: dict = Field(description='{"action": "approve"} | {"action": "edit", "sql": "..."} | {"action": "reject"}')


@app.post("/auth/login")
def login(req: LoginRequest) -> dict:
    p = authenticate(req.username, req.password)
    if p is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    return {"access_token": mint_token(p), "token_type": "bearer",
            "user": {"sub": p.sub, "role": p.role, "region_id": p.region_id}}


def _event_stream(p: Principal, thread_id: str, message: str | None = None, resume: dict | None = None):
    async def gen():
        yield {"data": sse({"type": "thread", "thread_id": thread_id})}
        try:
            async for ev in get_service().stream(p, thread_id, message=message, resume=resume):
                yield {"data": sse(ev)}
        except PermissionError as e:
            yield {"data": sse({"type": "error", "message": str(e)})}
        except Exception as e:  # noqa: BLE001
            log.exception("turn failed", extra={"user": p.sub})
            yield {"data": sse({"type": "error", "message": f"{type(e).__name__}: {e}"})}

    return EventSourceResponse(gen())


@app.post("/chat")
async def chat(req: ChatRequest, p: Principal = Depends(current_user)):
    thread_id = req.thread_id or f"{p.sub}:{uuid.uuid4().hex[:12]}"
    try:
        AgentService.check_thread(p, thread_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return _event_stream(p, thread_id, message=req.message)


@app.post("/chat/resume")
async def resume(req: ResumeRequest, p: Principal = Depends(current_user)):
    try:
        AgentService.check_thread(p, req.thread_id)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    return _event_stream(p, req.thread_id, resume=req.decision)


@app.get("/healthz")
async def healthz():
    s = get_settings()
    checks: dict[str, object] = {}
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            tags = (await client.get(f"{s.ollama_url}/api/tags")).json()
            checks["ollama"] = any(m["name"] == s.model_name for m in tags.get("models", [])) or f"model {s.model_name} not pulled"
        except Exception as e:  # noqa: BLE001
            checks["ollama"] = f"error: {e}"
        try:
            r = await client.get(s.mcp_url.rsplit("/mcp", 1)[0] + "/healthz")
            checks["mcp_server"] = r.status_code == 200 or r.text
        except Exception as e:  # noqa: BLE001
            checks["mcp_server"] = f"error: {e}"
    checks["agent"] = _service is not None
    ok = all(v is True for v in checks.values())
    return JSONResponse({"ok": ok, **checks}, status_code=200 if ok else 503)


@app.get("/")
def root():
    return RedirectResponse("/ui")


# Chainlit chat UI, served from the same process at /ui.
from chainlit.utils import mount_chainlit  # noqa: E402

mount_chainlit(app=app, target=str(Path(__file__).with_name("ui.py")), path="/ui")
