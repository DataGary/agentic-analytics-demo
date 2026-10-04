"""Thin async client for Cube's REST API. The caller's own JWT is forwarded, so Cube applies
the caller's scope (cube.js queryRewrite) rather than a service account's."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from common.config import get_settings


class CubeError(RuntimeError):
    pass


_client: httpx.AsyncClient | None = None
_meta_cache: tuple[float, dict] | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=get_settings().cube_url, timeout=30)
    return _client


async def load(query: dict[str, Any], token: str) -> list[dict]:
    # Cube answers "Continue wait" while a query is still running; poll until it's done.
    for _ in range(30):
        r = await client().post("/load", json={"query": query}, headers={"Authorization": token})
        body = r.json()
        if body.get("error") == "Continue wait":
            await asyncio.sleep(0.5)
            continue
        if r.status_code >= 400 or "error" in body:
            raise CubeError(str(body.get("error", body)))
        return body["data"]
    raise CubeError("Cube query timed out")


async def meta(token: str) -> dict:
    global _meta_cache
    if _meta_cache and time.time() - _meta_cache[0] < 300:
        return _meta_cache[1]
    r = await client().get("/meta", headers={"Authorization": token})
    if r.status_code >= 400:
        raise CubeError(r.text)
    _meta_cache = (time.time(), r.json())
    return _meta_cache[1]


async def ping() -> bool:
    r = await httpx.AsyncClient(timeout=5).get(get_settings().cube_url.replace("/cubejs-api/v1", "/readyz"))
    return r.status_code == 200
